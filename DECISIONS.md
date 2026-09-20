# DECISIONS.md

重要決策紀錄 — 依專案工作流程要求更新。

## 2026-09-20 — 第九輪（收尾）：修正第八輪審查退回的 3 MINOR + 3 NIT

使用者選了「做一輪小規模收尾，只處理這 6 項，然後補一次審查」。三項 MINOR 有共同形狀
——**宣告的保護大於實際的保護**：

| | 宣告 | 實際 |
|---|---|---|
| MINOR-1 | 鐵律註解說「呼叫 `_REAL_KILL_EXTERNAL()` 的地方都攔了 `subprocess.run`」 | 9 個呼叫點裡有 4 個沒攔（更正後為 3、再修完為 0；見上） |
| MINOR-2 | `_record_run()` 裡有 `isinstance(fired_map, dict)` 守衛，註解說「壞掉的 `_fired_today` 不會讓帳務炸掉」 | 守衛在**寫入端**，但先炸的是 `scheduler_loop()` 的**讀取端**，那行在守衛被執行**之前**就跑掉了 → 守衛**到不了** |
| MINOR-3 | 測試傳 `JOBSCAN_LOCK/LIVE/STATE` 指向 `/tmp` 以求隔離 | `job_board.py` 把這三個路徑**寫死**、不讀 env（只有 `run_scan.sh` 讀）→ 隔離是假的 |

**MINOR-2 的症狀值得單獨記**：`[scheduler] Error: 'list' object has no attribute 'get'`
每 30 秒一行（約 120 行/小時），而**面板照樣顯示漂亮的 `next_run`** ——
排程器停擺但 UI 看起來完全正常。可達路徑是工具與文件互相指向對方的正常操作：
`JOB_BOARD_INTERNAL_SCHEDULER=1`（文件記載的回復舊制路徑）+ 手改過的排程檔。
修法是把「什麼算合法的 `_fired_today`」定義在**唯一一個地方**（`_fired_map()`），
讀取端與寫入端共用它。

### 這一輪真正的教訓：**量尺自己會安靜地量錯東西**

修完之後重跑變異測試，得到 **30 個 `INCONCLUSIVE`**（`0P/0F rc=1`）。

判定是對的 —— 第八輪加的「測試檔必須跑到底」守衛**沒有**把它讀成「30 個逃脫」。
但真正的原因跟被測的程式無關：**我用 `python3 tests/mutate.py` 啟動載具，
而載具用 `sys.executable` 跑測試**，於是子行程是**系統 Python**，
`import job_board` → `from flask import …` → `ModuleNotFoundError`。

> `sys.executable` 的意思是「**你剛好用哪個解譯器啟動我**」，不是「這個專案該用
> 哪個解譯器」。生產（`run_scan.sh`）用的是 `$DIR/.venv/bin/python`，測試沒有
> 理由用別的。

修了兩件事，而**第二件才是結構性的**：

1. 解譯器改為固定取 `.venv/bin/python`（並留 `JOBSPY_MUTATE_PY` 測試鉤子，
   讓守衛本身可以被故意觸發 —— 一個觸發不了的守衛等於沒有守衛）。
2. **先跑一次未變異的基準線**，要求 `✅ 全數通過` 且 0 失敗，通過了才開始跑變異。

> **「量到 0」和「量尺壞了」在報表上長得一樣。** 兩者都是一張 30 列的數字表。
> 差別只在有沒有人先驗過量尺 —— 而這一輪是我自己差點沒驗。
> 基準線守衛的實測：`JOBSPY_MUTATE_PY=/usr/bin/python3` → **1 秒**內以
> 「✗ 基準線就不是全綠（0P/0F rc=1）」中止，並直接把
> `ModuleNotFoundError: No module named 'flask'` 印出來。

### 第三個實例：更正「宣告 > 實際」的那段文字，自己又犯了同一個錯

第八輪我在 `tests/test_scan_lock.py` 的鐵律註解裡補了一段更正，寫著
「`_REAL_KILL_EXTERNAL()` 共有 **9 個**呼叫點，其中 **3 個**沒有 mock ——
F4、C 區的兩處、以及 G2 的 TOCTOU」。這一輪用 AST 逐點重算才發現：

- 那是 **4 個項目掛在「3 個」底下**（清單與自己的計數不一致）；
- 而且 **F4 在寫那段話的時候已經修好了** —— 3 是修完 F4 之後的數字，
  4 才是寫下原句當時的數字，**兩件事被混成一句**。

AST 判定的結果（判準：這個呼叫點外面有沒有包著 mock `subprocess.run` 的 `with`）：

| 時點 | 呼叫點 | 沒被包住的 |
|---|---|---|
| 寫下「七個裡唯一一個」當時 | **9** 個 | **4** 個（302／311／472／739） |
| 補上 F4 之後（`5c036ee`） | 9 個 | 3 個（308／317／774） |
| 本輪修完（`26c1e39`） | 9 個 | **0** 個 |

> **同一個缺陷（宣告 > 實際）在三段先後「宣告它已經修好」的文字裡各犯了一次。**
> 教訓：「我列舉過了」不是證據 —— **列舉的結果要能被別人重跑**，
> 而且**重跑的方法本身不能把【註解】算成【實例】**。

我第一版的檢查腳本就是這樣爛掉的：`grep -n "_REAL_KILL_EXTERNAL()"` 在本檔 15 個
命中裡有 6 個是**註解**（其中一個是**鐵律註解本身在引用這個名字**），而我的腳本
甚至把鐵律註解裡那句 `mock.patch.object(jb.subprocess, "run", ...)` 當成了真的
mock —— 於是判定變成「全部都有攔」。**那跟 NIT-3（用 `systemctl show` 讀位置欄位、
把 Realtime 的值看成 Monotonic 的）是同一種病：把「提到」算成「實例」。**

### 本輪數字

- 測試：83 → **87** 項（C +1、G +1、L +2），全過、0 失敗。
- 變異：27 → **30** 個（M28 `_timer_next_text` 的「否認一件會發生的事」、
  M29 `kill_stalled_external` 的持鎖前置檢查、M30 `_fired_today` 不消毒）。
- `mutate.py` 的 `INCONCLUSIVE` 判定補記兩個已知界線（見該檔註解）：
  反向誤判是**保守**的（逼人來看，留著不修）；**非主執行緒崩潰是真正的盲區**
  —— 收尾標記由主執行緒印出，而這個專案的產品碼大量使用 daemon thread。



## 2026-09-20 — 第八輪（自查，不是審查退回）：保護的「宣告」與「實際」之間的落差

第七輪退回的修正做完、測試 65→81 全綠之後，我在重跑變異測試時看到一件不該發生的事：
**同一輪稍早還是「被逮」的變異 M4，變成「逃脫」了。** 追下去是兩個各自獨立、
但形狀完全相同的缺陷 —— **兩者都是「有一句話說某件事被保護著，而實際上沒有」**。

### 1. F4 是唯一沒有攔 `subprocess.run` 的 `_REAL_KILL_EXTERNAL()` 呼叫點

`tests/test_scan_lock.py` 裡有一段鐵律註解：

> 【往後新增測試的鐵律】任何直接呼叫 `_REAL_KILL_EXTERNAL()` 的地方都必須：
> 1. 用 `mock.patch.object(jb.subprocess, "run", ...)` 攔下所有 systemctl 呼叫 ——
>    systemd 分支的判斷依據是【生產 unit 的即時狀態】，這是唯一會漏出去的縫

**F4 沒有攔。** 而它是七個呼叫點裡唯一沒攔的（F4b、F4c、F7a–c、TOCTOU 全部攔了）。
那段註解聲稱的保護範圍與實際不符 —— 這正是**第四輪 MAJOR 的原句**，換一個地方復發。

> ⚠️⚠️ **2026-09-20 更正（第八輪審查）：上面那句話本身也是假的，而且錯了三層。**
>
> 審查員沒有採信我列的清單，自己重新列舉 —— 用 AST 逐一個問「這個呼叫點外面
> 有沒有包著 mock `subprocess.run` 的 `with`」，結果（`5c036ee~1`，也就是我寫下
> 那句話的當時）：
>
> | 時點 | `_REAL_KILL_EXTERNAL()` 呼叫點 | 其中【沒被包住】的 |
> |---|---|---|
> | 寫下那句話的當時 | **9** 個 | **4** 個（302／311／472／739） |
> | 補上 F4 之後（`5c036ee`） | 9 個 | 3 個（308／317／774） |
> | 本輪修完（`26c1e39`） | 9 個 | **0** 個 |
>
> 1. **總數錯**：不是七個，是九個。
> 2. **「唯一」錯**：當時沒攔的不是 1 個而是 **4 個** —— F4 之外，還有 C 區的兩處
>    與 G 區的 TOCTOU。
> 3. **最糟的是第三層**：我**點名 TOCTOU「攔了」**，而它正是那 4 個之一。
>    「列舉過」和「列舉對」是兩件事，我把前者當成了後者。
>
> **同一個缺陷（宣告 > 實際）在「宣告它已經修好」的那一段話裡又犯了一次** ——
> 這一則的標題是「保護的宣告與實際之間的落差」，而它自己就是那個落差的第三個實例。
>
> **可重跑的做法**（不要相信任何清單，包括這張表）：
>
> ```bash
> grep -n "_REAL_KILL_EXTERNAL()" tests/test_scan_lock.py   # 逐一往上找 mock
> ```
>
> ⚠️ 但 `grep` 會把**註解裡提到這個名字的行**也算進去（本檔 15 個命中裡有 6 個是
> 註解），而我的第一版檢查腳本又進一步把**鐵律註解裡那句
> `mock.patch.object(jb.subprocess, "run", ...)` 當成了真的 mock** —— 因為它也是
> 一行符合字串的內容。於是判定變成「全部都有攔」。
> **那跟 NIT-3（讀位置欄位）是同一種病：把「提到」算成「實例」。**
> 唯一可靠的是走 AST：

```python
# 逐一問：這個 _REAL_KILL_EXTERNAL() 呼叫點，外面有沒有包著 mock subprocess.run？
import ast, subprocess
src = subprocess.run(["git", "show", "HEAD:tests/test_scan_lock.py"],
                     capture_output=True, text=True).stdout
tree = ast.parse(src)

def is_mockrun(w):                      # 只看真的 with 陳述，不看註解
    s = ast.unparse(w)
    return "patch.object" in s and "subprocess" in s and '"run"' in s

calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name) and n.func.id == "_REAL_KILL_EXTERNAL"]
parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

def enclosing(c):
    out, n = [], c
    while n in parent:
        n = parent[n]
        if isinstance(n, (ast.With, ast.AsyncWith)) and is_mockrun(n):
            out.append(n.lineno)
    return out

unmocked = sorted(c.lineno for c in calls if not enclosing(c))
print(f"呼叫點 {len(calls)} 個；沒被包住的 {len(unmocked)} 個：{unmocked}")
```

後果分兩層：

**(a) 真的差一點殺掉生產的掃描。** F4 用 `trigger="systemd-timer"`，所以 phase 閘門
一旦失效（變異 M4），它會一路走到：

```
systemctl --user kill --signal=SIGKILL jobscan.service
```

實際攔下它的是**最底層那把「argv 含 `kill` 就拋例外」的跳線**。那把跳線的設計意圖
是「只在寫錯時響」的最後一道防線，而它真的響了。當時生產的 `jobscan.service` 正好
在 `activating` —— 06:00 那一輪的補跑（見下方第 4 點），不是假想情境。

**(b) 更陰險的是它把「被逮」變成了「逃脫」。** 跳線拋的例外會讓**整個測試檔當場崩潰**，
而崩潰的行程 `[FAIL]` 數是 **0**；`tests/mutate.py` 當時的判定是
`if failed > 0: 被逮 else: 逃脫`，於是 M4 被記成「逃脫」。

> **最嚴重的變異看起來最無害，而且判定會隨生產 unit 當下的狀態翻來翻去。**
> 這就是這個專案已經被燒過三次的那種數字。

修法兩邊都補：
- F4 補上 `subprocess.run` 的攔截，並新增一項斷言「連讀的 `show` 都不該被呼叫」。
- `tests/mutate.py` 在判定前先確認測試檔**印出了收尾標記**（`✅ 全數通過` 或
  `項失敗：`）。沒有＝沒跑到底 → `INCONCLUSIVE`，**不是逃脫**，且讓退出碼非 0。

實測：修好之後 M4 在生產 unit **正在 `activating`** 的當下跑，得到 `79P/4F`（被逮），
不再崩潰、不再漂移 —— 而且**同一輪稍早它在同樣的生產狀態下是「逃脫」**，
差別只在測試檔有沒有跑到底。判定不再取決於生產 unit 當下的狀態。

**這一則的教訓**：那段鐵律註解寫得很對，而且**就寫在違反它的那段程式碼上方 400 行**。
**寫在註解裡的規則不會自己執行。**

### 2. 附帶實測：`list-timers` 的 NEXT 顯示 `-` 不是幽靈排程

同一輪順手查證的。`systemctl --user list-timers jobscan.timer` 在**每一輪掃描正在跑的時候**
（一天兩次、各約 19 分鐘）`NEXT` 欄是 `-`。乍看正是本專案的主題：畫面說沒有下一次。

用暫時性 unit 做了可重跑的對照（自己建、自己收，不碰 jobspy 的 unit）：

```bash
systemd-run --user --unit=jwprobe2 --on-calendar='*-*-* *:*:00' /bin/sleep 30
```

| 狀態 | `NEXT` | `NextElapseUSecRealtime` | `NextElapseUSecMonotonic` | **JSON `next`** |
|---|---|---|---|---|
| timer 尚未觸發、service 未跑 | `Sun 09:14:00 AEST` | `Sun 2026-09-20 11:22:00 AEST` | **`0`** | **`1789859640000000`（int）** |
| timer 尚未觸發、service **手動**拉起 | `Sun 09:14:00 AEST` | 同上 | **`0`** | 同上 |
| **timer 已自行觸發**、其 service 仍在跑 | **`-`** | **（空）** | **`infinity`** | **`None`（null）** |
| service 結束後 | `Sun 09:14:00 AEST` | 下一次的值 | `0` | 下一次的值 |
| 生產 `jobscan.timer`（08:49:02→09:07:55） | `-` | （空） | `infinity` | — |

> ⚠️ **2026-09-20 更正（第八輪 NIT-3）：上一版這張表把第三欄寫成「有值」，
> 那是錯的。** 我當初是用 `systemctl show` 讀**位置欄位**——而它印的是**固定的
> 規範順序**（`Realtime` 在前、`Monotonic` 在後），所以我把 Realtime 的值看成了
> Monotonic 的值。實測第一列是 `Realtime=Sun …11:22:00 AEST` / **`Monotonic=0`**。
> 現在改成用 `systemctl show …-p NextElapseUSecRealtime -p NextElapseUSecMonotonic`
> **逐項指名**，讓這種誤讀在結構上不可能。

**而更正之後看到的事情比原本記的更有用：這兩欄不是同一個值的兩種視圖，值會搬家。**

| | `NextElapseUSecRealtime` | `NextElapseUSecMonotonic` |
|---|---|---|
| 尚未觸發（下次觸發是一個**牆鐘時刻**） | 有值 | `0` |
| 已觸發、下次還沒算（**不再等**） | （空） | `infinity` |

哪一欄有值，取決於 systemd 當下**用哪個時鐘在想「下一次」**。所以 `infinity`
不是「一個很大的牆鐘時間」，而單獨看 `Monotonic=0` 也不是「1970-01-01」。

> 🔴 **這正是我寫錯的那一格的實質後果，不是美醜問題**：`get_timer_state()` 若改用
> `systemctl show` 取 `NextElapseUSecMonotonic`（看起來最像「倒數」的那個名字），
> 正常狀態下拿到的是 **`0`** → 會被當成 `1970-01-01` 的「下次觸發」。
> **我在文件裡犯的誤讀，就是程式碼換一個屬性是會犯的同一個錯。**
> 文件寫錯一格只是難看；程式碼讀錯一欄會讓面板顯示一個過去的時間，而它看起來像答案。

**⚠️ 上面第二列才是生產的實際狀態，而它教我一件比原本以為更精確的事：**
觸發條件**不是「service 正在跑」**，而是**「timer 的那次 elapse 已經被消費掉、下一次還沒算出來」**。
第一列是反例 —— service 明明在跑，`NEXT` 卻好好的，因為那個 service 是我手動拉的，
timer 的那次 elapse 還在。**生產的 `jobscan.timer` 是自己觸發 service 的**，所以它落在第二列。

**結論：正常且會自己恢復。** 不是幽靈排程。

**而這個 probe 順便替 `_timer_next_text()` 的修正做完最後一哩驗證**：`get_timer_state()`
是走 `list-timers --output=json` 並取 `u.get("next") or None`。第二列量到的是
**`None`（JSON null）**，不是 `0`、也不是 `INT64_MAX`。所以：
- `0` 這種值不會出現 → 不會有「`1970-01-01` 被當成下次觸發」的假答案；
- `INT64_MAX` 不會出現 → 不會有「幾億年後」的假答案；
- `next_iso` 就是 `None` → **新分支真的會被走到，不是死碼**。

> 這一格如果沒量，`_timer_next_text()` 就是**一段看起來很合理、但可能永遠不會執行的
> 修正** —— 而「有修正、沒有守衛」正是這個專案前三輪反覆抓到的形狀。
> 差別只在於這次的守衛是**一行 probe 的輸出**，不是一個測試。

**但同一個時間點 `TimersCalendar` 的 `next_elapse=` 是【已經過去的值】**（08:49 時
06:00 那行仍顯示 `Sun 2026-09-20 06:00:00`，服務結束後才變成 `Mon 2026-09-21 06:00:00`）。
所以它**不能**拿來當 `next` 的退路 —— 那會把一個過去的時間標成「下次觸發」，
比空著更糟，因為它看起來像一個答案。

**據此修掉一個反方向的缺陷**：啟動 banner 原本無條件印
`下次觸發: {next_iso or '(無)'}`。`next_iso is None` 其實有**兩種意思**——
「timer 沒裝」與「掃描執行中、systemd 還沒算」。一律印成「(無)」的話，若看板剛好在
掃描窗口內重啟，日誌上會留下一句讀起來像「排程壞了」的話，而真相是 22:00 一定會跑。

> 幽靈排程是**承諾一件不會發生的事**；這一則是**否認一件會發生的事**。
> 方向相反，但都是「畫面與事實不符」，都值得修。

判定邏輯抽成 `_timer_next_text()` 以便測試（同 `_record_run()` / `_compute_next_run()`
的手法）。前端本來就是對的：`next_run` 為 null 時它**省略**「下次 …」那一段，
而不是顯示一個錯的值。

**⚠️ 本輪審查對這一節的更正（NIT-2）**：我原本在這裡寫「這個修正的守衛是一行
probe 的輸出，**不是一個測試**」。**那是錯的** —— 它是測試套件裡的正式一項
（`banner 的『下次觸發』必須區分…`）。審查員把它改回修正前，那一項真的 FAIL。

**真正的缺口是別的：它沒有對應的＊變異＊。**
`grep -c "_timer_next_text" tests/mutate.py` 當時是 **0** —— 也就是說「這個測試是
load-bearing 的」這件事**從來沒有被變異實驗證明過**。而本專案自己的規則就寫在
`deploy/README.md`：「若你新增修正卻找不到會失敗的變異，代表那個修正沒有被測試
覆蓋」。

> **「有測試」與「測試是 load-bearing 的」是兩件事。** 只有變異能區分它們 ——
> 一個永遠會通過的測試與一個不存在的測試，在測試報告上長得一模一樣（都是綠的）。
> 本輪已補上 **`M28`**（把 `_timer_next_text()` 退回無條件「（無）」），實測被逮。
>
> 順帶：連**我自己在寫下這個更正的時候**都在猜「是測試還是 probe」——
> 所以判斷一句話對不對的方式是**去跑它**，不是去想它。

### 3. 生產證據：休眠喚醒後 `Persistent=true` 真的補跑了一輪

**這是整個遷移的原始需求，2026-09-20 在生產環境觀測到了**（不是 lab）：

| 時刻 | 事件 |
|---|---|
| 04:30 / 06:00 | 機器睡著，兩個 timer 的時段都被錯過 |
| 08:48:59 | VM 跟宿主機喚醒 → `systemd-resolved: Clock change detected`（連續兩筆） |
| 08:48:59 / 08:49:01 | `jobscan.timer`、`jobboard-logrotate.timer` **各自補跑**（stamp mtime 為證） |
| 08:49:03 → 09:07:55 | 掃描實際執行 18 分 52 秒，`exit_code=0` |
| 09:07:58 | 看板**自動偵測並切換**到新結果檔（40 筆） |

這一次同時把三個原本只有 lab 證據的路徑變成生產證據：`Persistent` 的休眠補跑
（原本是刪 stamp + drop-in 造假時刻）、`logrotate` 由 timer 觸發（原本 LAST 是 `-`）、
`copytruncate` 之後 dashboard 仍寫進新檔。

**仍未證的一條**：機器**醒著**時的正常排程觸發（22:00 到點、機器沒睡）。補跑走的
是 `Persistent` 那條；兩者在 systemd 裡是同一個 `timer_enter_waiting()` 算出的 elapse，
但**這是推論，不是觀測**。詳見 `deploy/README.md` 的「實機驗證記錄」一節。

### 4. 修正後的完整變異表（83 項檢查，27 個變異）

| 變異 | 結果 | | 變異 | 結果 |
|---|---|---|---|---|
| M1 回收判準改回 `proc is not None` | ✅ 82P/1F | | M15 回復舊制時不算 `next_run` | ✅ 82P/1F |
| M2 拿掉 `try/finally` | ✅ 82P/1F | | M16 `times` 不驗證（回復幽靈排程） | ✅ 80P/3F |
| M3 拿掉 `errors=replace` | ✅ 82P/1F | | M17 `_valid_times` 不檢查內容 | ✅ 78P/5F |
| **M4 拿掉 phase 閘門** | **✅ 79P/4F**（第七輪曾假逃脫） | | M18 拿掉主體型別檢查 | ✅ 82P/1F |
| M5 拿掉 cmdline 身分檢查 | ✅ 81P/2F | | M19 `enabled` 用 `bool()` 寬鬆轉換 | ✅ 82P/1F |
| M6 拿掉 `ActiveState` 前置檢查 | ✅ 81P/2F | | **M20 記在「跑完那天」而非「觸發那天」** | **✅ 81P/2F** |
| M7 拿掉 `_external_begin` 身分閘門 | ✅ 81P/2F | | M21 第三道鎖失效 | ✅ 82P/1F |
| M8 `kill_stalled_external` 整個 no-op | ✅ 78P/5F | | M22 `interval_hours` 漏 `OverflowError` | ✅ 82P/1F |
| M9 拿掉 TOCTOU 重檢 | ✅ 82P/1F | | M23 `_valid_times` 用 `len+isdigit+int` | ✅ 82P/1F |
| M10 `_pid_is_our_scan` 退回子字串比對 | ✅ 82P/1F | | M24 損壞排程檔靜默退回預設值 | ✅ 81P/2F |
| M11 `_out` 退回 `print()`（無鎖） | ✅ 80P/3F | | M25 post-run `next_run` 用「現在」算 | ✅ 81P/2F |
| M12 保留鎖但用 `print()`（兩次 write） | ✅ 80P/3F | | M26 `api_schedule` 一律回 `ok:true` | ✅ 82P/1F |
| **M13 拿掉鎖（整行一次 write）** | **❌ 逃脫（已知）** | | M27 `_parse_timer_calendar` 編一個時段 | ✅ 82P/1F |
| M14 「回復舊制」分支整個不執行 | ✅ 82P/1F | | | |

`26/27 被逮、1 個已知逃脫、0 個 INCONCLUSIVE`，且**生產檔案的 sha256 前後不變**
（`518e51207de4341c…`）—— 變異跑在 `git archive` 出來的隔離副本裡。

**M13 為什麼還是逃脫（這一條從第三輪留到現在）**：它拿掉的是 `_out()` 的
`threading.Lock`。那是為了讓多執行緒交錯時**整行一次寫出**；而 M13 同時保留了
「整行一次 write」那一半，所以只剩「兩條執行緒可能在同一行中間插隊」這種
**競態**，而競態在單一行程的測試裡是機率性的。它的觀測方式是**人工**的
（`deploy/README.md` 有記：連續跑 N 次、檢查有沒有 `[scheduler][jobscan]` 這種黏行），
不是自動測試。這一條**已知且已理解**，不是漏掉。

---

## 2026-09-20 — 第七輪審查退回：跨午夜的幽靈排程，與一個「測不到」的藉口

第七輪 Senior Reviewer `[REVIEW_REJECTED]`：3 MAJOR + 7 MINOR + 6 NIT。

### MAJOR-1 — 幽靈排程從第二條路徑復發：跨午夜的掃描會吃掉隔天早上那一輪

`a6f77a6` 消滅的幽靈排程（面板承諾一件不會發生的事）回來了，走的是另一條路。

掃描跑完之後，`scheduler_loop()` 用**跑完的當下**（`now2`）回推「最近 6 小時內最接近的
時段」，來決定剛剛燒掉的是哪一個。22:00 起跑、00:30 才結束的那一輪跨過午夜，
於是 `now2` 的日期已經是**隔天**，而：

```
|00:30 − 06:00| = 5.5h < 6h   →   把【隔天早上 06:00】記成已觸發
```

隔天 06:00 的 due 判定是 `if t in already_fired: continue` —— **直接跳過，連一行 log
都沒有**。而面板算出來的 `next_run` 正是 06:00。**畫面承諾一件永遠不會發生的事，
而且沒有人收到錯誤。**

**修法的重點不是把推導改對，是不要推導。** due 判定 `break` 的當下就已經知道是哪一天
的哪一個時段了 —— 把它傳進新的模組層級純函式 `_record_run()` 就好。

> 「推導錯」在結構上不再可表達，這比「推導對了」強。

### MAJOR-2 — README 的檢查數表格自己加起來不等於總數

`deploy/README.md` 寫 `| H | 3 |`，全表加總 66，而權威量測是 65。實際是 H=2、I=1、J=2
—— **J 區的兩項沒有印自己的 `=== J. ===` 標題**，用 `awk` 依標題分組的計數器會把它們
算進前一個標題 H。已更正並加註：**要引用項數請用 `grep -c '\[PASS\]'`，不要用分組數字相加。**

### MAJOR-3 — SIGABRT 的 C 格重測不出來

我發佈的 `33/340 ≈ 10%` 審查員重測得到 `36/200 = 18%`。補上 Wilson 信賴區間之後
那不是「不穩定」，而是**兩個互相矛盾的量測**（z=2.79，p=0.0053）。詳見本檔
「`deploy/README.md` 記載的復原程序是虛構的」（2026-09-20，第三輪～第五輪之間那一則）
底下的 SIGABRT 更正區 **(a2)**：C 那一格不該有數字，只有範圍；而且 **B 與 C 誰高誰低
從來沒有證據**（p=0.076），我卻把它們列成兩列不同數字，讓讀者自己補上一個
不存在的排序。**唯一站得住的是「A 遠小於 B 與 C」**（三個對照 p<0.0001）。

### MINOR m1–m7：四個真缺陷 + 三個覆蓋缺口

| | 缺陷 | 為什麼是缺陷 |
|---|---|---|
| m1 | `interval_hours` 的 `except (TypeError, ValueError)` 接不住 `OverflowError` | `issubclass(OverflowError, ValueError)` 是 **False**；JSON 的 `1e400` 解析成 `inf` → `int(inf)` → 逃出 `api_schedule` → **HTTP 500**，而瀏覽器端的 catch 會吞掉它（使用者看到「什麼都沒發生」） |
| m2 | `_valid_times` 用 `len+isdigit+int` | `'⁰⁶'.isdigit()` 是 **True** 但 `int('⁰⁶')` 丟 `ValueError`（驗證器自己丟例外）；`'０６'` 更糟 —— 兩者都成功，於是**靜默放行**一個 `strptime` 解不開的字串，排程器每 30 秒記一次錯誤（約 120 行/小時的日誌膨脹） |
| m3 | `load_schedule()` 壞檔時 `except: pass` | 使用者的設定被**靜默**換成預設值。現在保留原檔為 `.corrupt` 並回報 |
| m4/m5 | 前端 `times[1] \|\| '22:00'` | 單一元素的清單會顯示一個**永遠不會觸發**的第二格 —— 與 `times: []` 是同一類幽靈排程，只是方向相反 |
| m6 | `api_schedule` 的 `ok` 永遠是 `true` | 「一半的欄位被拒絕」也回報成功 —— API 合約上的不實。現在 `ok` ＝「這個請求被聽懂了，而且每個欄位都照要求生效了」 |
| m7 | `_compute_next_run` 的 interval 分支沒有任何斷言 | 它只被 K 區間接碰到，K 只看 `mode`／`interval_hours` 有沒有存進去，沒看 `next_run` |
| N5 | 面板把「每日 06:00 / 22:00（Melbourne）」**寫死在 HTML 裡** | 現在剛好是對的，但改 timer 不會改面板 → 面板又開始承諾一個不存在的時段。改讀 `systemctl show -p TimersCalendar`；讀不到就顯示「讀不到時段設定」，**不編一個出來** |

> **m2 我自己第一版就寫錯了**：`re` 的 `\d` 預設匹配 **Unicode 數字**，所以
> `r"([01]\d|2[0-3]):[0-5]\d"` 會放行 `'0۶:00'`（阿拉伯-印度數字 6）。
> 是我自己新寫的測試當場抓到的 —— 改用顯式 `[0-9]` 字元類別。

### M20 從 `EXPECTED_ESCAPES` 移除

M20 原本是「排程器跑完後回到未排序的 `times[0]`」，被列為**已知逃脫**，理由寫在
`tests/mutate.py` 裡：

> 這一段【結構上測不到】—— 它在 `scheduler_loop()` 裡、`INTERNAL_SCHEDULER` 為真時
> 才會執行，而且必須等一輪 19 分鐘的真掃描結束。

**審查員指出那個理由不成立** ——帳務邏輯本來就可以抽成純函式來測，而那段「測不到」
的程式碼裡就藏著 MAJOR-1。抽出 `_record_run()` 之後，L 區測得到它，M20 也重新設計成
`_record_run()` 的**日期那一半**（記在「跑完那天」而非「觸發那天」），實測 `79P/2F` 被逮
（當時 81 項檢查；第八輪擴到 83 項後為 `81P/2F`，見上一則）。

> **「測不到」通常只是「還沒抽出來」的另一種說法。**
> 留著那個豁免，就等於把一個 MAJOR 藏在「已知且已理解」的清單底下。

至於舊版那個「挑錯時段」的另一半，現在**結構上不可表達** —— `_record_run()` 不再
推導剛剛燒掉哪個時段，它**接收**那個時段。所以那個方向的變異沒有對應的 target 可以改，
這是刻意的，也寫在 `tests/mutate.py` 的註解裡。

## 2026-09-20 — 幽靈排程、繞過護欄的爬蟲，與一個「兩次不算證據」的驗證

第六輪審查 `[REVIEW_REJECTED]`：2 MAJOR + 3 MINOR + 2 NIT。這一輪的四個發現
形狀各異，但都指向同一件事：**「有測試」與「測試在保護那個行為」是兩回事。**

### 1. MAJOR M2 — J 區的隔離不是護欄，是一個常數

J 區用子行程驗證啟動時的排程主權轉移，靠 `_J_TRIPWIRE` 阻止它真的去殺生產的
掃描。那道跳線只包了 `subprocess.run`，擋 argv 含 `"kill"` 的呼叫。

但排程器啟動爬蟲走的是**完全不同的路**：

```python
subprocess.Popen([sys.executable, "-u", "linkedin_job_search.py"])
```

**`Popen` 不是 `run`，完全繞過那道跳線。** 所以第六輪之前的隔離不是護欄，而是
「種子排程的 `interval_hours` 剛好很大」這個**常數** —— 種子一逾期，子行程就會在
**生產目錄**跑起真的爬蟲。那一輪真的發生過兩次（`job_board.log` 兩筆
`[scheduler] Triggering search`）。審查員用原封不動的跳線實測重現。

修法：再加一道 `subprocess.Popen` 跳線，argv 含 `linkedin_job_search.py` 就拋
`AssertionError`。**兩道都必須裝在 `import job_board` 之前** —— `job_board` 在
模組層就啟動背景執行緒，插在其後有 race。

這裡的教訓比缺陷本身重要：**「我擋住了 X」和「我擋住了通往 X 的所有路」是兩個
不同的宣稱。** 前者可以靠讀程式碼確認，後者只能靠列舉。我當時只做了前者。

### 2. MINOR m2 — 幽靈排程：重構把「大聲的失敗」變成了「靜默的失敗」

`api_schedule` 的 POST 對 `times` 照單全收。`times: []` 存進去之後：

| 路徑 | 對 `times: []` 的解讀 |
|---|---|
| `_compute_next_run()`（面板、啟動） | `cfg.get("times") or [...]` —— 空清單是 falsy → **退回預設時段** |
| `scheduler_loop()`（真正決定要不要跑） | `cfg.get("times", [...])` —— 只有 key 不存在才退回 → **for 迴圈空轉** |

於是面板顯示「06:00 會掃描」，而排程器**永遠不會掃描**。畫面承諾了一件不會
發生的事，而且沒有人會收到錯誤。

**這是重構製造出來的**：舊碼在同樣輸入下是 `IndexError` → HTTP 500 —— 醜，但
大聲。把吵的失敗變成靜默的失敗，正是這個專案一直在獵捕的方向，所以補上入口驗證。

修法：`_valid_times()` 驗證格式（`'HH:MM'`、24 小時制、非空清單、元素皆為字串），
`mode`／`interval_hours`／`times`／`enabled` **四個欄位一律「驗證過才落地」**，
不合法就保留原值並回報 `warning`。核心不變式：**`next_run` 永遠由【已儲存的】
cfg 推導** —— 幽靈排程的定義就是這條不變式被破壞。

順帶修掉兩個同類的：
- 非 JSON 物件的主體（`null`／`[]`／`"x"`）在舊碼是 `"enabled" in data` → `TypeError`
  → HTTP 500。改成 400 —— 500 是大聲的，但 400 才是這個請求真正的意思。
- `bool("false")` 是 `True`。舊碼寬鬆地 `bool()` `enabled`，於是把**「關掉排程」
  執行成「打開排程」**。只接受真正的 bool。

還有一個：**後端從第六輪開始回報的 `warning`，前端從來沒有顯示過。** 請求被拒絕
與存檔成功在畫面上長得一模一樣（面板顯示舊值，而舊值正是使用者剛想改掉的東西）。
`updateSchedule()` 現在會把 `warning`／`error` 顯示出來。

### 3. MAJOR M1 — 兩次不是證據（我在同一輪犯了兩次抽樣錯誤）

第五輪我發佈了「變異表可重現」，根據是**同 HEAD 跑兩次**。審查員跑第三次就抓到
M5 漂移（`56P/2F` ↔ `57P/1F`）。

根因是一個 race：`bystander.poll() is None` 緊接在 SIGKILL 之後。**SIGKILL 送出與
子行程真的被 reaped 之間有窗口**，在窗口內 `poll()` 回 `None` —— 於是「該被殺」
的變異體**假通過**。20 次裡 2 次。改成有界等待（`wait_dead(bystander, 0.5)`：
等它真的死，再斷言它沒死）之後，M5 連跑 20 次都是 `56P/2F`，而且**比修正前多
逮到一項**。

> **同一個錯誤我在同一輪犯了兩次**：先用 2 次樣本宣稱變異表穩定，再之前在 J 區
> 用 3 次樣本宣稱測試穩定（實際 20 次裡只有 14 次全綠）。**抽樣不是驗證。**
> 這一輪的變異表跑了**四次**才下結論。

### 4. 測試「崩潰」與測試「失敗」在變異表上長得一樣

這是我在寫 K 區時自己踩到的。`mutate.py` 數的是 `[FAIL]`，而 `check()` 只有在
被呼叫時才印 `[FAIL]`。一個讓測試**在跑到斷言之前就 traceback 死掉**的變異會得到
0 個 FAIL → 記成逃脫。

具體：K 區用 `mock.patch.object(jb, "jsonify", side_effect=lambda **k: k)`，而產品碼
寫的是 `return jsonify(resp)`（**位置**引數）→ `TypeError` → 整個測試檔死在 K 區
第二項，後面全部不執行。**這個 bug 讓 M16~M19 全部變成假逃脫。**

修法兩層：(a) 測試碼自己把例外收成 dict（`_post_schedule` 的 `except Exception`
回 `{"__error__": ...}`，於是例外是一項 FAIL 而不是一次崩潰）；(b) 任何「讀檔／
呼叫產品碼」的輔助函式一律不讓例外冒出去（`_startup_schedule` 的排程檔讀取同理）。
**看到變異逃脫時，先確認測試是「跑完之後有 FAIL」還是「根本沒跑完」。**

### 5. 順帶：`scheduler_loop` 的 `times[0]` 排序問題（見上一則的更正）

兩條路徑對同一個 cfg 有不同解讀，是第 2 點那個幽靈排程的同一個病根。第六輪一併
對齊成 `sorted(cfg.get("times") or [...])`。**但沒有回歸保護** —— 那段要
`INTERNAL_SCHEDULER=1` 且先跑完一輪真掃描。變異 `M20` 就是它，**保證逃脫**，
刻意列進 `EXPECTED_ESCAPES`。

### 6. 盤點時發現的第五個缺口：第三道鎖從來沒有測試

補完 m2 之後，用新蓋好的 `_post_schedule()` 把 `api_schedule` 的每一條分支走一遍，
發現**三道鎖裡唯一擋得住 curl／devtools 的那道**（`want and not INTERNAL_SCHEDULER`
→ 拒絕並回 warning）**到第六輪為止沒有任何測試**。前端唯讀只是 UI 層的禮貌，
這道才是真正的閘門；少了它，兩條觸發路徑同時存在＝2026-08-13 的並發事故。
補上 K7（含反向控制：**關閉必須永遠有效**，否則這道鎖會順手把「停用排程」也擋掉）
與 M21。

**這不是被審查員抓到的，是自己盤點出來的** —— 而盤點的契機是為 m2 蓋了一個
「可以在測試行程裡直接 POST」的鷹架。**能便宜的造出輸入，就會看見原本看不見的分支。**

### 變異表（第六輪，65 項檢查，21 個變異）

`tests/mutate.py`，每個變異 = 「把某項修正改回去」，看測試會不會響。
最終版本（含 M21）在乾淨副本連跑**兩次**，逐項、逐數字相同，退出碼 0，
生產檔案 sha256 不變。加上 M21 之前的四次，這一輪總共跑了**六次**。

| 變異 | 結果 | 變異 | 結果 |
|---|---|---|---|
| M1 回收判準改回 `proc is not None` | 64P/1F ✅ | M12 保留鎖但用 `print()` | 62P/3F ✅ |
| M2 拿掉 `try/finally` | 64P/1F ✅ | M13 拿掉 `_LOG_LOCK` | 65P/0F ❌ 已知逃脫 |
| M3 拿掉 `errors="replace"` | 64P/1F ✅ | M14 回復舊制分支不執行 | 64P/1F ✅ |
| M4 拿掉 phase 閘門 | 63P/2F ✅ | M15 回復舊制時不算 `next_run` | 64P/1F ✅ |
| M5 拿掉 cmdline 身分檢查 | 63P/2F ✅ | M16 `times` 不驗證 | 63P/2F ✅ |
| M6 拿掉 `ActiveState` 前置檢查 | 63P/2F ✅ | M17 `_valid_times` 不檢查內容 | 62P/3F ✅ |
| M7 拿掉 `_external_begin` 身分閘門 | 63P/2F ✅ | M18 拿掉主體型別檢查 | 64P/1F ✅ |
| M8 `kill_stalled_external` no-op | 60P/5F ✅ | M19 `enabled` 寬鬆 `bool()` | 64P/1F ✅ |
| M9 拿掉 TOCTOU 重檢 | 64P/1F ✅ | M20 排程器跑完後回到未排序 `times[0]` | 65P/0F ❌ 已知逃脫 |
| M10 `_pid_is_our_scan` 退回子字串比對 | 64P/1F ✅ | M21 第三道鎖失效 | 64P/1F ✅ |
| M11 `_out` 退回 `print()` | 62P/3F ✅ | | |

**19/21 被逮，2 個已知逃脫。** M13（拿掉 `_LOG_LOCK`）與 M20 都不是「沒被發現的
覆蓋缺口」，而是「已理解且接受」—— 兩者的理由都寫在 `mutate.py` 的
`EXPECTED_ESCAPES` 旁邊。

## 2026-09-20 — `deploy/README.md` 記載的復原程序是虛構的；以及一個隨機紅的測試

第六輪施工。兩件事都是同一個形狀：**文件或綠燈讓你以為某件事成立，而它不成立。**

### 1. 「回復到舊制」是靜默失敗（不是文件過期，是文件寫的事情沒發生）

`deploy/README.md` 的「回復到舊制」一節寫著：停掉兩個 timer、在 `jobboard.service`
加 `Environment=JOB_BOARD_INTERNAL_SCHEDULER=1`、重啟。這是這次遷移規劃時**刻意
保留**的退路（「停用但保留」），也是風險表最後一列的復原方案。

實測（2026-09-20）發現照做之後：

| 欄位 | 結果 | 後果 |
|---|---|---|
| `schedule.enabled` | 停在 `false` | 排程器執行緒啟動了，但**永遠不觸發** |
| `schedule.managed_by` | 仍是 `"systemd-timer"` | 前端 `SCHEDULE_READONLY=true` → 面板唯讀，**UI 也救不回來** |
| `schedule.next_run` | 沒算 | 面板「下次執行：—」 |

log 只印了 `[scheduler] Scheduler thread started` —— 看起來完全正常。
使用者得到的是「再也不會掃描，而畫面顯示排程由一個剛剛被停用的 timer 管理」，
只能手改 `.job_board_schedule.json`。**那份復原計畫等於是虛構的**，而它之所以
沒被發現，是因為它從來沒被走過。

修法：啟動時把 `managed_by` 設回 `internal`、`enabled` 還原、`next_run` 算出來，
並印一行 `[scheduler] Internal scheduler ENABLED`。

刻意選「自動啟用」而不是「把面板改成可編輯、讓使用者自己按」：設定這個環境變數
的語意就是「我要舊制」，而舊制＝排程會運作。留成一個需要人再按一次才能動的狀態，
等於把同一個坑換個位置。並發由 `run_scan.sh` 的 `flock` 吸收。

**這裡的教訓與第三輪的 `kill_stalled_external` 完全一樣**：一個從來沒被走過的路徑，
「程式碼還在」不等於「功能還在」。差別是這次連測試都沒有，只有一份文件在保證它。

### 2. 順帶修掉的 drift：`next_run` 的兩份複製品

`next_run` 原本在兩個地方各自計算（UI 儲存、啟動回復舊制），而**排程器跑完那條
是第三份**。實際上已經 drift：UI 那條用 `sorted(times)`，排程器那條用 `times[0]`
（未排序）。`times` 若被存成 `["22:00","06:00"]` 且當天時段都已過，排程器會把
下一次算成「今天 22:00」（已過）而不是「明天 06:00」。

抽成 `_compute_next_run()` 之後 UI 與啟動兩條共用（都沿用 `sorted()`）。
**排程器迴圈那條刻意不改** —— 它要用「跑完的當下」而不是「現在」來算，語意不同；
那個 `times[0]` 的排序問題仍待處理，記在 `job_board.py` 的 docstring 與本檔的
已知限制。重構前先取了四個 POST 案例的基準輸出，重構後逐字比對相同。

### 3. MAJOR（自己踩到）：J 區的兩個檢查有 ~10% 機率隨機失敗

新加的 J 區（子行程驗證啟動時的排程主權轉移）在 **20 次基線裡只有 14 次全綠**。
我第一次跑 3 次都過就往下走了 —— 那 3 次是運氣。

根因不是斷言，是**子行程的死法**：`python -c "import job_board"` 會在模組層啟動
watchdog / jobscan 監看 / 排程器三個 daemon 執行緒，而 `-c` 一結束主執行緒就進入
interpreter finalization，那些執行緒還在寫 stdout：

```
Fatal Python error: could not acquire lock for
<_io.BufferedWriter name='<stdout>'> at interpreter shutdown,
possibly due to daemon threads
Python runtime state: finalizing
```

→ SIGABRT（rc = -6）。實測 forward 25 次中 3 次、reverse 25 次中 2 次。
斷言本身一直是對的，是**子行程的死法**讓 `returncode != 0` 那條分支被走進去。

修法：子行程結尾 `os._exit(0)` 跳過 finalization。不削弱這一區 —— 排程檔是
import 期間同步寫完的，斷言讀磁碟內容；`save_schedule()` 若沒被呼叫，讀回來的
就是 seed 本身，兩項都會 FAIL（M14/M15 已驗證）。

**這不是生產缺陷，已查證**：`systemctl stop` 送 SIGTERM 時 Python 沒裝 handler
（實測 `signal.getsignal(SIGTERM)` 回 `0` = `SIG_DFL`），核心直接終止、不跑
finalization。journal 從 2026-09-19 至今 0 筆 ABRT/core-dump，七次重啟全乾淨。
只有在「直譯器**正常結束**」時才會踩到 —— 也就是這個測試寫法本身。

> ⚠️ **2026-09-20 更正（我自己發佈過兩次的數字，兩次都被推翻）**：
>
> **(a) 上面的 3/25 與 2/25 是錯的** —— 我自己重測不出來，審查員也重測不出來。
> 那些數字來自一個我沒有描述清楚的子行程組態，而**沒有描述組態的數字不是證據**。
> 所以量測本身進了版控：`tests/sigabrt_probe.py`（三個組態、可調 N、強制前置檢查）。
> 四個獨立量測（N = 60／60／20／200），合計：
>
> | 組態 | 合計 | 比率 | Wilson 95% CI |
> |---|---|---|---|
> | A ＝ 現在的子行程**拿掉** `os._exit(0)`（保留 `flush()`）、管線 | **3/340** | **0.9%** | **[0.3%, 2.6%]** |
> | B ＝ `d80fb93` 的原始版（沒有 flush、沒有 `os._exit(0)`）、管線 | **48/340** | **14.1%** | **[10.8%, 18.2%]** |
> | C ＝ 同 B，但 stdout 導到**檔案**（我測） | **33/340** | **9.7%** | **[7.0%, 13.3%]** |
> | C ＝ 同 B，但 stdout 導到**檔案**（**審查員測**） | **36/200** | **18.0%** | **[13.3%, 23.9%]** |
>
> **(a2) 只有「A 遠小於 B 與 C」站得住；B 與 C 的排序從來沒有證據。**
> 第七輪審查 MAJOR-3 指出我的 C 格（33/340 ≈ 10%）重測不出來（審查員 36/200 = 18%）。
> 補上信賴區間之後，那其實是**兩個互相矛盾的量測**：
>
> | 比較 | z | p | 結論 |
> |---|---|---|---|
> | A vs B | 6.55 | <0.0001 | **分得開** |
> | A vs C（我測） | 5.14 | <0.0001 | **分得開** |
> | A vs C（審查員測） | 7.42 | <0.0001 | **分得開** |
> | **C（我測）vs C（審查員測）** | **2.79** | **0.0053** | **互相矛盾** |
> | B vs C（我測） | −1.78 | 0.076 | **沒有證據** |
>
> 也就是說：**C 的比率不是那個組態的性質，而是當下環境的性質**（負載、排程時機、
> 直譯器版本/build、終端機 vs 檔案……我沒有定位出是哪一項，所以這裡只能誠實說
> 「它會變」）。所以這一格**不該再有一個數字**，該寫的是一個範圍：
> **C ≈ 7%~24%**，而且**B 與 C 誰高誰低未知**（我原本把兩者列成兩列不同數字，
> 那個排序 p=0.076，等於沒有）。
> **唯一不受影響的是 A**：三個對照全部 p<0.0001，因為它的機制不同（不進 finalization）。
>
> **(b) 我第二次的更正也是錯的 —— 「光靠 `flush()` 就夠」不成立。**
> 我當時根據兩次 0/60 就發佈「A = 0」，但 N=20 那次出現了 1 次 ABRT。
> A ≈ 1%：看起來很小，但一輪測試會 spawn 這個子行程好幾次，**隨機紅會留下來**。
> **真正讓它結構上不可能的是 `os._exit(0)`**（根本不進 finalization，也就不需要
> 去搶那個鎖）。兩個都留著是對的 —— 修法沒變，變的只是「為什麼」。
> 同理，「管線比檔案更容易踩到」（我根據 15/60 vs 3/60 下的結論）也不成立：
> N=200 那兩組是 23 vs 22。**兩組 60 次的樣本差異，小於量測本身的 run-to-run 變異。**
>
> **(c) 「不是生產缺陷」的推論不完整。** 原本只查了 `systemctl stop` 那條路；
> 生產**還有**正常結束的路徑（werkzeug `serve_forever` 攔到 `KeyboardInterrupt`、
> 埠被佔用時的 `SystemExit`），審查員實測那兩條 6/6 rc=-15、20/20 rc=1，ABRT 0 次。
> 真正的護欄是 unit 的 `Environment=PYTHONUNBUFFERED=1`：本機實測它讓
> `sys.stdout.buffer` 是 **`FileIO`**（未設時是 `BufferedWriter`），而那個 fatal
> error 點名的正是 `BufferedWriter`。**拿掉 `PYTHONUNBUFFERED=1`，整段推理失效。**
>
> **(d) 這一則本身就是這個專案最好的反面教材**：同一個數字我錯了三次，每一次
> 都是因為「樣本數不足以支撐那個結論，而我照樣下了結論」。第三次錯在**修正
> 第二次的錯誤時**——用兩次 0/60 去證明一個「是 0」的主張。
> **「是 0」比「不是 0」難證明得多；要宣稱某件事不會發生，需要的是機制
> （`os._exit(0)` 不進 finalization），不是樣本。**

**連帶影響**：這件事讓「變異表的數字可重現」這句話一度變成假的（同一份程式碼
連跑兩次得到不同的 P/F）。修掉之後連跑兩次逐項相同。

> ⚠️ **2026-09-20 更正**：「連跑兩次逐項相同」**不足以**下這個結論 —— 審查員
> 跑第三次就抓到 M5 漂移（`56P/2F` ↔ `57P/1F`）。真正的原因是第六輪才找到的
> race：`bystander.poll() is None` 在 SIGKILL 送出後、子行程真的死掉前會回 `None`，
> 於是「該被殺」的變異體**假通過**。改成有界等待（`wait_dead(bystander, 0.5)`）
> 之後，M5 連跑 20 次都是 `56P/2F`。**兩次不是證據。**

> ⚠️ **這已經是這個專案第四次「綠燈不代表有效」**：
> 第三輪 `kill_stalled_external` no-op 後 39/39 全過、第四輪 `errors="replace"`
> 拿掉也全過、第五輪 H 區沒逮到它自己要保護的修正、I 區 AST 掃描把 `_out` 整段
> 豁免導致根本沒開火。這次是第五種形狀：**測試會響，但隨機不響**。
> 前四次的解法是「把修正改回去看它會不會響」；這次那個方法不夠 ——
> **要連跑很多次**。單次綠燈的資訊量比想像中低。

### 已知限制（本輪新增）

- **排程器跑完後算 `next_run` 那段仍用未排序的 `times[0]`**（見第 2 點）。
  只在內建排程器啟用時才會走到，目前主權在 timer，所以擱著。**沒有測試覆蓋。**

  > ⚠️ **2026-09-20 更正：第六輪已經修掉了**，兩條路徑現在都是
  > `sorted(cfg.get("times") or [...])`（連 `or` 也對齊，所以舊版寫進檔案的空清單
  > 在兩邊都會退回預設時段）。**但「沒有測試覆蓋」這半句仍然成立** —— 那段程式碼
  > 要 `INTERNAL_SCHEDULER=1` 而且得先跑完一輪真掃描才會執行。變異 `M20` 就是
  > 這一段，**保證逃脫**，刻意列進 `EXPECTED_ESCAPES` 讓缺口是機器看得見的事實。

- **任何新寫的子行程測試若 import `job_board`，都必須以 `os._exit()` 收尾**，
  否則會得到隨機紅的測試。已寫進 `deploy/README.md` 的踩坑清單。

## 2026-09-20 — 變異工具在生產主機上、以及三個「有修正沒守衛」的補強（第五輪審查）

第五輪 Senior Reviewer 對 `46a8fff` 判定 **`[REVIEW_PASSED]`**，附 1 個 MAJOR 與
5 個 MINOR。它沒有只讀文件：自己重跑了測試與變異工具、逐格比對變異表、
用生產等價設定重現了黏行的因果。

### MAJOR — `tests/mutate.py` 在 `Restart=always` 的生產主機上有載入變異的窗口

**這是我自己造的，而且與第四輪 MAJOR 是同一個形狀 —— 只是換個入口。**

原本 `mutate.py` 直接改寫 repo 裡的 `job_board.py`，跑完再用 `git checkout` 還原。
13 個變異每個會在磁碟上存在 5–25 秒（整輪約 2 分鐘）。而 `jobboard.service` 是
`Restart=always` + `MemoryMax=512M`（unit 註解本身就預期會被 OOM 殺掉）。若這段
窗口內看板被殺掉而重啟，**新行程載入的就是那個變異** —— 而變異清單裡正好有：

| 變異 | 內容 | 後果 |
|---|---|---|
| M5 | `_pid_is_our_scan` 恆真 | watchdog 會殺掉任何符合形狀的行程 |
| M6 | 拿掉 `ActiveState` 前置檢查 | 對非 active 的 unit 也送 kill |
| M8 | `kill_stalled_external` 整個 no-op | 停滯偵測完全失效 |

（M6 那個尤其重：`systemctl --user kill --signal=SIGKILL <unit>` 對 **inactive**
的 unit 回傳 0 但什麼都沒殺 —— 是第四輪就量過的假成功。）

**修法：不在生產目錄裡跑。** 改成 `git archive HEAD | tar -x` 解到 `/tmp` 的隔離
副本，在那裡產生並執行變異。生產的 `job_board.py` **從頭到尾不會被寫入**，結束時
以 sha256 驗證（`生產檔案驗證：sha256 不變 ✓`）。還原機制也不再需要 `git checkout`
—— 每個變異都是從記憶體裡的 `ORIG` 重新產生，磁碟上沒有中間狀態可殘留。

> 為什麼選隔離而不是「偵測到 jobboard 在跑就拒絕執行」：後者需要操作者記得繞過，
> 而看板幾乎永遠在跑，於是那個 guard 只會變成「每次都加 `--force`」。**讓危險的
> 做法在結構上不可能，比要求人記得避開可靠。**

### MINOR-1 — `_out()` 的註解漏了必要前提，而且原子性的措辭不精確

審查用**有緩衝**的檔案實測：`print` 在 3200+2400 行、8 執行緒下 **0 黏行** ——
一度以為診斷是錯的。真正讓它現形的是 unit 裡的 `Environment=PYTHONUNBUFFERED=1`
（`deploy/jobboard.service`、`jobscan.service` 都有）：**不緩衝才會把 print 的兩次
write 變成兩次真正的 write(2) syscall**，交錯才是必然。同條件對照：

| `PYTHONUNBUFFERED=1`、O_APPEND 檔案、8 執行緒 | `print` | 單次 write |
|---|---|---|
| 短行 (~120B) | **139 / 2400 黏** | 0 / 4800 黏 |
| 12 KB 行 | **314 / 2043 黏** | 0 / 4800 黏 |

**我的註解沒寫這個前提，這是真的缺漏** —— 有人拿掉 `PYTHONUNBUFFERED` 或把日誌
改成 `StandardOutput=journal`，註解描述的因果就不再成立，而沒有測試會提醒。

同時更正原子性的措辭：**不是** O_APPEND 本身給了原子性（它只保證 offset 設到
檔尾）。不交錯來自 Linux 核心對同一 inode 的 write(2) 以 `i_rwsem` 序列化；
**POSIX 對一般檔案並沒有保證這件事**（`PIPE_BUF` 的保證只適用 pipe/FIFO，且只到
4096 bytes）。並把「鎖不是 load-bearing」的結論**限縮在今天的部署前提**：日誌
目標是本地檔案、行長遠小於 PIPE_BUF（實測 `job_board.log` 最長行 203 bytes、
`cron_search.log` 353 bytes）。改成 pipe/journal 且行長超過 4096 時鎖就會變成
load-bearing —— 但審查**未能實測重現**那個情境（四種實作在真實 pipe 下都 0 黏行），
所以那是理論風險、不是已知事實。

### MINOR-2／MINOR-3 — 補上兩個守衛，其中一個是我自己寫壞的

**MINOR-2**：H 區只觀測 `_log()` 這條路徑。具體失敗情境：有人在 scheduler 或
watchdog 執行緒加一行 `print("[timer] ...", flush=True)` → 56 項全過 → 黏行回來
（審查實測該寫法在生產設定下黏 139/2400 行）。新增 **I 區 AST 掃描**：`job_board.py`
出現任何 live `print(` 或 `_out()` 外的直接 `sys.stdout.write` 就 FAIL。

> 用 AST 而不是 grep：`grep` 分不出「程式碼」與「註解／docstring」—— `_out()` 上方
> 就有一大段說明 print 為什麼不能用的註解，grep 會把它們全部算成違規（偽陽性），
> **然後就會有人把這個測試關掉**。

**MINOR-3**：H2 原本是測不出東西的煙霧測試（M11 在它底下照樣通過）。改用會讓出
GIL 的假 stdout（`write()` 內 `time.sleep(0)`），並把斷言從「串接後 split」改成
**逐次檢查 `write()` 呼叫**。後者才是決定性的：`_out()` 對每行只呼叫一次，
所以每一次呼叫都必須是「一整行、含結尾換行」；`print` 會產生「內容（無換行）」與
「裸的 `\n`」兩個呼叫，兩個都不符合。

### ⚠️ 我自己在這一輪又犯了一次同樣的錯，而且是被變異測試抓到的

I 區第一版把「落在 `_out` span 內」當成**整段豁免**，結果 M11（把 `_out` 的內容
換成 `print(line, flush=True)`）的那個 `print` 也在 span 內，被一起放行了 ——
**這一項根本沒開火。**

發現方式不是重讀程式碼，是對照 **M11 的失敗項數**：照理 H1、H2、I 三項都該響，
卻只有 2 個 `[FAIL]`。修好之後 M11/M12 都變成 3 個 `[FAIL]`。

> **教訓：變異測試不只驗證修正，也驗證了驗證本身。** 「測試通過」在這一輪之內
> 已經是第三次不代表「有保護」—— 前兩次是 M11 逃脫、H2 測不出東西。
> 唯一能區分的動作永遠是同一個：**把修正改回去，看它會不會響。**

### MINOR-4 — `mutate.py` 本身的四個洞

| 洞 | 修法 |
|---|---|
| 乾淨度檢查只涵蓋 `job_board.py`，但判定同時取決於測試檔 | 兩個檔案都檢查 |
| `SYNTAX-ERR` 走「不計」且不影響退出碼 → 套用失敗的變異讓整輪看起來是綠的 | 計入 `BAD`，退出碼變 1 |
| 逾時被算成「被逮」→ 變異造成死鎖時結論反轉 | 改判 `INCONCLUSIVE`，退出碼變 1 |
| `EXPECTED_ESCAPES` 用標籤前綴比對，任何以 `M13` 開頭的新標籤都會被自動豁免 | id 與標籤分開，綁明確 id；並新增「**豁免過期**」警告（列在 `EXPECTED_ESCAPES` 卻被逮到 = 有人補了測試，該把它刪掉） |

最後一項是為了讓這份清單不會腐化成裝飾品 —— 一個只增不減的豁免清單，
跟一份只增不減的「已知問題」文件一樣，遲早會蓋掉真正的訊號。

### 已知限制（本輪未修，誠實列出）

- **`linkedin_job_search.py` 有同一個兩次 syscall 的結構**（56 個 `print`，
  `jobscan.service` 同樣 `PYTHONUNBUFFERED=1`）。今天沒有症狀（`_worker` 不 print，
  `cron_search.log` 用雙時戳啟發式檢查 0 筆），但若爬蟲改多執行緒輸出、或
  `run_scan.sh` 的 echo 與爬蟲的 print 交錯，同一個缺陷會在另一份日誌復發。
  **I 區的 AST 掃描只涵蓋 `job_board.py`，沒有涵蓋爬蟲。**
- **看板重啟後 `/api/search/status` 的 `last_finished` / `run_id` / `exit_code` /
  `start_time` 回 `None`**，但 `logs/search_state.json` 裡有。診斷用的 in-memory
  `_EXTERNAL["finished"]` / `_LAST_SCAN_SOURCE` 沒有從磁碟回填。
  `data_age_seconds` 是從檔案 mtime 算的，**不受影響**；前端的 `exit_code` 徽章
  在 `null` 時只是不顯示，不會顯示錯誤結論。用 `git log -S` 追出是 `081d6b5`
  帶進來的**既有**行為，非本次遷移引進。審查同意「不影響排程與資料，不該在
  這個遷移裡再疊加新變數」，故保留待後續處理。

## 2026-09-19 — 測試會殺掉生產掃描、殺戮路徑沒有正向測試（第四輪審查）

第四輪 Senior Reviewer 對 `56cef73`（第三輪 MAJOR 的修正）判定 `[REVIEW_REJECTED]`。
兩個 MAJOR 有同一個形狀：**修正本身沒有被任何測試守住**；而其中一個是
**測試親手犯下這一輪正在修的那個錯**。

### MAJOR — 回歸測試會對【生產】unit 送出真的 SIGKILL

`tests/test_scan_lock.py` 的「第二道閘門」區塊用 `trigger="systemd-timer"`，而且
**沒有 mock `subprocess.run`**。systemd 分支的判斷依據是

```
systemctl --user show jobscan.service -p ActiveState --value
```

的**即時值** —— 所以只要測試跑到那裡時真的有一輪在跑（unit 在 06:00／22:00 各約
19 分鐘處於 `activating`，任何手動掃描也算），測試就會送出
`systemctl --user kill --signal=SIGKILL jobscan.service`，**殺掉使用者正在跑的那一輪**。

**為什麼「安全護欄」沒擋住**：檔頭的護欄把 `jb.kill_stalled_external` 換成 no-op，
但測試本文是直接呼叫**先前存下來的** `_REAL_KILL_EXTERNAL()` —— 換掉模組全域的
**名字**擋不住已保存的**函式參照**。註解聲稱的保護範圍與實際不符，這比漏洞本身更
該修：**一個說謊的護欄會讓下一個人不再去檢查**。

**修法（三層，第 3 層才是關鍵）：**

1. 該區塊改走 PID 路徑（`trigger="manual"`、pid 指向測試自己生的誘餌）——
   而且它**本來就該走這條**。systemd 分支是刻意殺掉整個 unit，根本不經過 cmdline
   身分檢查；用 systemd 分支去測 cmdline 檢查等於**什麼都沒測到**。
2. `mock.patch.object(jb.subprocess, "run", ...)` 攔下所有 systemctl 呼叫。
3. **結構性跳線**：把 `jb.subprocess.run` 包一層，argv 含 `"kill"` 就當場拋例外。
   合法用途（F7b）本來就會 mock 掉，**根本走不到這裡** —— 所以跳線只在寫錯時響，
   不需要任何 opt-in 開關，也就沒有「忘記開」或「忘記關」的問題。
   修一行只能修這次；換掉底層的 syscall 才能防止下一次。

### MAJOR — 殺戮路徑完全沒有正向測試：把功能整個停掉也能 39/39 全過

覆蓋探針把 `kill_stalled_external()` 改成開頭 `return None`（整個功能死掉），
**39 項檢查全數通過**。原因是 F 區每一條斷言都是「**不得**開火」的形式，於是測試
分不出「正確地拒絕」與「永遠不開火」。

這特別危險，因為**「永遠不開火」正是這一輪修正自己引進的新風險**：矯正過度之後，
卡死的掃描要等 5 小時（`TimeoutStartSec`），而 `trigger=manual` 的手動掃描**根本
沒有 unit 可以等**，卡死是無界的（見下方「已知限制」）。

**修法**：補上正向覆蓋 —— F7a（身分相符 → 必須開火，且目標**真的死掉**，
`poll()=-9`）、F7b（unit `activating` → 必須送出 kill）、F7c（unit `inactive` →
**不得**送出）。

> F7c 補的是我自己差點弄丟的覆蓋：MAJOR 的修法把 F4b 從 systemd 分支移到 PID
> 路徑，於是「unit 不在跑時不得送出 kill」這條就沒人守了 —— 拿掉 ActiveState
> 前置檢查的變異（M6）會直接逃脫。**修一個洞的時候要問：這一動讓哪條斷言失去了
> 唯一的守衛。**

### MINOR — 兩個「有修正、沒有回歸保護」的缺口

- `errors="replace"`：拿掉它（退回 strict 解碼）39/39 照過。這正是第三輪 MAJOR-A
  的成因，卻沒有任何東西守著。→ G1 直接斷言 Popen 的 kwarg。
- TOCTOU 重檢：拿掉「動手前再確認一次沒有取得鎖」39/39 照過。→ G2 用 `side_effect`
  讓 `_we_hold_scan_lock()` 第一次回 False、第二次回 True，斷言必須放棄。

### MINOR — `_pid_is_our_scan()` 子字串比對太鬆，而測試把這個弱點寫成了規格

舊判準是 `"run_scan.sh" in cmdline or "linkedin_job_search.py" in cmdline`，所以
`vim run_scan.sh`、`tail -f run_scan.sh` 全部算「我們的掃描」。更糟的是 F3 用
`exec -a linkedin_job_search.py sleep 600` 造了一個冒名者，然後**斷言它必須被認可**
—— 等於把缺陷固化成規格。

要送出的訊號是 SIGKILL（**不可逆**），所以寬鬆的方向剛好是最危險的那一邊。

新判準讀 **argv 結構**，不讀字串內容：

| argv 形狀 | 判定 |
|---|---|
| `argv[0]` basename == `run_scan.sh` | 是 |
| `argv[0]` 是 shell 且 `argv[1]` basename **就是** `run_scan.sh` | 是 |
| `argv[0]` 是 python 解譯器且**參數中**有 `linkedin_job_search.py` | 是 |
| 其餘（含 `vim`／`tail` 帶著同樣的路徑） | 否 |

> 爬蟲的 `argv[0]` 是 **`python`** 而不是腳本本身，因為 `run_scan.sh` 用
> `"$PY" -u "$SCRIPT"` 啟動它。這一點寫錯就會變成「永遠不開火」，所以 F3 同時加了
> 兩個**正向對照組**（真爬蟲形狀、真 wrapper 形狀都必須被認出）。少了正向對照組，
> 把函式改成永遠回 False 也會全部通過。

### 測試衛生：兩個讓訊號失真的測試缺陷（不是產品缺陷）

第四輪修完後跑變異測試，M8 逾時 400 秒。診斷結果：**測試本身 6 秒就跑完了** ——
是兩個 PPID=1 的孤兒 python 握著管線的寫入端。子行程會**繼承測試的 stdout**，
而 stdout 是 `... | tail`；應該被殺的誘餌活下來（正是 M8 製造的情境）就等於
管線永遠等不到 EOF。留著孤兒也等於每跑一次變異就在機器上疊一個 `sleep 600`。

接著發現更嚴重的：F7a 斷言「應該被殺」之後接**無界等待**，突變讓誘餌活著時直接拋
`TimeoutExpired`，**F7b / F7c / G 共 9 項根本沒跑到**。變異表面上「被逮」，實際是
把後面的訊號全遮掉 —— 真實迴歸發生時後果相同（只看得到第一項失敗，看不到全貌）。

**修法：**

- 誘餌一律走 `spawn()`：統一 `DEVNULL`、全部登記，收工無條件清掉（`atexit` +
  正常路徑各一次）。失敗路徑**最需要**這個，而失敗正是變異測試刻意製造的情境。
- **本檔鐵律：不得對誘餌呼叫無界的 `.wait()`。** 逾時一律走 `wait_dead()`，它把
  `TimeoutExpired` 轉成「回傳 False」，於是**逾時本身變成一項 FAIL 而不是例外**。
- 效果可量測：M8 的輸出從 **44 項 / 1 FAIL（中途炸掉）** 變成
  **53 項 / 5 FAIL（完整跑完）**。變異測試的價值來自「一次看到全部失敗」，
  中途炸掉會把 5 個訊號壓成 1 個。

### 變異測試工具：兩個會產生「假安全感」的陷阱

1. **假變異**：M7 原本錨在 `_external_begin()` docstring 的一句註解上。改掉註解
   **不可能改變行為**，所以這個變異永遠不會 FAIL、永遠「逃脫」—— 看起來像
   「這個修正沒有回歸保護」，實際上是我根本沒改到程式。改錨在真正的閘門
   （`if state.get("phase") != "running":` → `if False:`）之後才有效。
   **凡是「逃脫」的變異，第一個要懷疑的是變異本身，不是測試。**
2. **殘留檢查不能用 grep**：上一版變異腳本中止時把 M8（`kill_stalled_external`
   整個 no-op）留在檔案裡，而我的殘留檢查是 `grep -c 'MUTANT\|if True:'` ——
   **M8 的變異是一個裸的 `return None`，grep 不到**。差一點帶著「停滯偵測完全
   失效」的版本繼續往下做。

   現在改成：啟動前先確認工作區乾淨（否則「還原」會還原到錯的東西）、還原一律
   `git checkout -- job_board.py`（**commit 是唯一可信的已知良好狀態**，不依賴
   `finally` 有沒有跑到）、攔 SIGTERM/SIGINT 就地還原、結束時用 `git diff --stat`
   驗證還原，而不是比對樣式。

### 順帶修掉：日誌行會因執行緒交錯而黏在一起

在生產的 `job_board.log` 實際看到：

```
[23:39:24] [watchdog] Watchdog thread started[23:39:24] [jobscan] External scan watcher started======
```

`print()` 是**兩次** write()（先內容、再換行）。stdout 在 systemd 的
`StandardOutput=append:` 下是 O_APPEND 的檔案，watchdog 執行緒與 jobscan 監看
執行緒（還有啟動 banner）交錯時，換行就落到別人的文字後面，兩行黏成一行。
23:05 那次啟動正常、23:39 那次黏住 —— 是間歇性 race。

**這正好抵銷 MINOR 2.4 的目的**：加時戳就是為了讓順序可對齊，黏行比沒有時戳更難追。

**修法**：`_out()` 對每一行只呼叫一次 `write()`（O_APPEND 下單次 write 具原子性）。
所有可能被背景執行緒呼叫的 `print()` 都改走它 —— watchdog、jobscan 監看、
scheduler 執行緒，以及 **Flask 請求執行緒**（`get_timer_state`／`load_jobs`／
`start_search` 都可由 request handler 觸發）。啟動 banner 也改了，它正是當事者之一。

**過程中被自己抓到兩次**：

1. 第一版只換掉兩個多行 `print()` 的**第一行**，把尾端的 `flush=True` 留著 ——
   `_out()` 當場 `TypeError`，scheduler 執行緒直接死掉。**`py_compile` 會過，
   只有真的 import 才看得出來。**
2. 第一版的回歸測試**沒有逮到修正前的實作**（見下）。

### ⚠️ 一個「有修正、沒有回歸保護」的實例，而且是我自己犯的

H 區第一版用 8 執行緒 × 40 行對 `StringIO` 猛寫，斷言沒有黏行。**結果把 `_out()`
退回修正前的 `print()`（變異 M11）之後，55/55 全數通過** —— 這正是本專案被咬過
兩次的形狀，而我剛剛才在同一份文件裡寫下那條教訓。

原因：`StringIO` 太快，GIL 在兩次 `write()` 之間幾乎不切換，race 逼不出來。
而把競爭拉高到會不定期失敗就變成「不穩定的測試」—— 本專案也吃過那個虧。

**改法：斷言 race 的【成因】，不賭 race 本身。** `print()` 一定拆成兩次 write()，
這是確定的；於是「每行只呼叫一次 write()」就等價於「黏行結構上不可能」，
而且是決定性的。用一個記錄呼叫次數的假 stdout 來驗。

同時，原本那個併發測試**保留但重新標示**：它是行完整性的一般性煙霧測試，
**不是** 這個缺陷的守衛（M11 也會通過它）。繼續宣稱它有保護作用，
就是同一種錯用小包裝再犯一次。

### 變異測試結果（`tests/mutate.py`，15 個變異，14 被逮、1 已知逃脫）

變異工具從 `/tmp` 搬進版控（`tests/mutate.py`），**上面每一個「被逮」的宣稱
都可以用一行指令重跑**。

數字為 2026-09-20 第五輪的實跑快照（測試 58 項，**連跑兩次逐項相同**）。

> ⚠️ **2026-09-20 更正：這張表已被本檔最上方第六輪的版本取代，而且「連跑兩次
> 逐項相同」是錯的結論**（M5 有 race，第三次就漂移；理由見最上方條目）。
> 這裡保留原數字當歷史快照 —— 會被取代的是數字，不是「每個修正都要有會失敗的
> 變異」這個要求。

| 變異 | 內容 | 結果 |
|---|---|---|
| M1 | 孤兒回收判準改回 `proc is not None` | 57P / 1F ✅ |
| M2 | 拿掉 `_read_search_output` 的 try/finally | 57P / 1F ✅ |
| M3 | 拿掉 `Popen(errors="replace")` | 57P / 1F ✅ |
| M4 | 拿掉 `kill_stalled_external` 的 phase 閘門 | 56P / 2F ✅ |
| M5 | 拿掉 cmdline 身分檢查 | 57P / 1F ✅ |
| M6 | 拿掉 systemd 分支的 ActiveState 前置檢查 | 56P / 2F ✅ |
| M7 | 拿掉 `_external_begin` 身分閘門 | 56P / 2F ✅ |
| M8 | `kill_stalled_external` 整個 no-op | 53P / 5F ✅ |
| M9 | 拿掉 TOCTOU 重檢 | 57P / 1F ✅ |
| M10 | `_pid_is_our_scan` 退回子字串比對 | 57P / 1F ✅ |
| M11 | `_out` 退回 `print()`（修正前行為、無鎖） | 55P / 3F ✅ |
| M12 | 保留鎖但用 `print()`（兩次 write） | 55P / 3F ✅ |
| M13 | 拿掉鎖、保留單次 write | 58P / 0F ⚠️ **已知逃脫** |
| M14 | 回復舊制的分支整個不執行（`else:` → `elif False:`） | 57P / 1F ✅ |
| M15 | 回復舊制時不算 `next_run` | 57P / 1F ✅ |

> ⚠️ **這張表的 PASS/FAIL 數會隨測試項數變動**（測試從 55 加到 56 之後，
> M1 就從 54P/1F 變成 55P/1F）。**權威來源是 `tests/mutate.py` 的實跑輸出**，
> 這張表只是某一次的快照；會變的數字不是重點，**判定欄（被逮／逃脫）才是**。
>
> ⚠️ **但「數字會變」不等於「數字可以漂移」。** 2026-09-20 發現 J 區有一項
> 隨機失敗（見本檔最上方條目），同一份程式碼連跑兩次得到不同的 P/F ——
> 那時候「這張表只是快照」這句話變成了掩蓋不穩定的藉口。
> **同一個 HEAD 連跑兩次必須逐項相同；不同就是有東西不穩定，要去查。**
> 要重跑：
>
> ```bash
> .venv/bin/python tests/mutate.py     # 需乾淨的工作區；在 /tmp 隔離副本裡跑
> ```
>
> 這是第四次記錄到「文件裡的數量聲明會腐化」（28→39→53→55→56）。
> 每次改動測試就順手重跑一次，比記得回來改文件可靠。

> M10 只被**一項**逮到，就是 F3 反轉後的那條「冒名者必須被拒」。這是第四輪把 F3
> 從「斷言冒名者必須被認可」改成「必須被拒絕」的直接價值 —— **把弱點寫成規格的
> 測試，比沒有測試更糟**，因為它會主動阻止別人修。

> **M13 逃脫是刻意記錄下來的，不是漏掉。** 它證明**那把 `_LOG_LOCK` 不是
> load-bearing**：黏行的成因是 `print()` 的兩次 write()，單次 write 就足以讓它
> 結構上不可能（M12 被逮、M13 逃脫，兩者合起來正好把因果釘死）。
> 鎖留著是 defence in depth，**但目前沒有任何測試能證明少了它會出問題**。
> 「兩個都必要」的原註解是錯的，已更正。
>
> `mutate.py` 用 `EXPECTED_ESCAPES` 把「已知且已理解的逃脫」與「沒被發現的覆蓋
> 缺口」分開回報 —— 混在一起會讓這個工具失去訊號。**已知逃脫不影響退出碼，
> 但一定會印出來，且必須在這裡有對應說明**（就是這一段）。
>
> ⚠️ 2026-09-20 第六輪新增 **M20**（`scheduler_loop` 跑完後回到未排序的
> `times[0]`）到這份清單。**它是刻意加進來的**：那條路徑要
> `INTERNAL_SCHEDULER=1` 而且得先跑完一輪 19 分鐘的真掃描，測試結構上碰不到。
> 加這個變異的用意是**把覆蓋缺口變成機器看得見的事實**，而不是文件裡的一句
> 安慰 —— 它的說明同時寫在 `mutate.py` 的 `EXPECTED_ESCAPES` 與本檔最上方。

### 已知限制（本輪未修，誠實列出）

**stray holder 造成的卡死是無界的。** `trigger=manual` 的手動掃描沒有對應的
systemd unit，`TimeoutStartSec=5h` 不適用；而 stray holder（例如有人
`flock logs/jobscan.lock -c 'sleep'`）持鎖時，`run_scan.sh` 印 `SKIPPED`、**exit 0**、
且刻意不覆寫 state —— 於是 state 停在上一輪的 `finished`，
`kill_stalled_external()` 的 phase 閘門直接 return，**沒有任何機制會放掉那把鎖**。

這不是本輪引進的退步（修正前也殺不到真正的 holder，它拿的是 state 裡已被回收或
已死的舊 pid，只會殺錯人並留下假成功），但 DECISIONS.md 原本寫的「不殺的代價有界
—— 真正卡死的掃描仍會被 5h 逾時收掉」**只對一半，不能當成通則**，已在該處加註更正。

真正的解法是照本專案自己的原則 ——「鎖由誰持有是唯一可信的事實」—— 用
`/proc/*/fd` 反查誰開著 `JOBSCAN_LOCK`，再對那個 pid 驗 cmdline，而不是信任 state
裡的 pid。同一招也能順帶解掉 cmdline 比對的偽陽性問題。

**觸發前提是人為佔用**：爬蟲本身沒有 subprocess／multiprocessing，不會留下漏繼承
fd 9 的孫行程（這點第三輪審查已獨立確認），所以嚴重度不是 CRITICAL。

**看板重啟後，閒置狀態會遺失「最近一次是哪一輪」。**
`get_search_status()` 的閒置分支（`job_board.py:1076`）註解寫著「閒置時仍要回報
『最近一次是哪一輪』，否則前端的換檔偵測會失去依據」，但 `_EXTERNAL["finished"]`
與 `_LAST_SCAN_SOURCE` 都是**行程內記憶體狀態**，`Restart=always` 每次復活都會清空。

2026-09-19 重啟後實測 `GET /api/search/status`：

```
running=False  run_id=None  exit_code=None  start_time=None  last_finished=None
```

而 `logs/search_state.json` 明擺著 `run_id=20260919_220000, exit_code=0,
finished_at=2026-09-19T22:21:45` —— **資訊在磁碟上，只是沒人去讀**。

**影響有限，所以本輪刻意不修**：

- 前端換檔偵測需要**先觀察到** `running=true`（`sawRunning`）才會動作，
  所以不會誤觸重載；`lastHandledRunId` 的比對也不會被 `None` 騙到。
- 安全相關的「資料已 N 小時未更新」用的是 `data_age_seconds`，那是**獨立**從
  `search_results/` 的檔案 mtime 算出來的（實測 4679 秒 ≈ 78 分，正確），
  **不依賴任何記憶體狀態**。也就是說真正要緊的訊號不受這個缺口影響。

**不修的理由不是「不重要」，而是時機**：本專案已經有兩次「修正的旁邊就是下一個
缺陷」的紀錄（第三輪的 MAJOR-A／MAJOR-B 就是第二輪修正自己引進或沒關掉的）。
在送審前插入一個未經審查的行為變更，正是前兩輪退件的成因。**列為已知缺口，
交由審查判斷輕重。**

真要修的話，方向是在閒置分支以 `read_jobscan_state()` 為**唯讀後備**
（state 檔由 `run_scan.sh` 原子寫入，是「最近一輪」的權威來源），並且必須
**連同回歸測試一起**——本專案已經吃過兩次「有修正、沒有回歸保護」的虧。

### 文件更正

- 測試數：原稱「28 項」→ 第三輪實測 **39** → 本輪修正後 **53**。
- 第三輪審查的 FAIL 數：原稱「3 項」→ 審查實測 **5 項**。
- 兩處都加 `> ⚠️ 更正` 註記，**不改寫歷史**。
- `deploy/README.md` 補上「測試覆蓋（誠實聲明）」一節。

> **教訓**：這兩個數字都是我憑印象寫的。文件裡的數量聲明如果沒有把**產生它的
> 指令**一起記下來（`grep -c '\[PASS\]'`），就一定會腐化 —— 而腐化的數字比沒有
> 數字更糟，因為它會被相信。

## 2026-09-19 — 掃描鎖的兩個 MAJOR：孤兒鎖盲區、誤殺無關行程與假成功（第三輪審查）

第三輪 Senior Reviewer 對 `8853e68` 判定 `[REVIEW_REJECTED]`，獨立重跑變異實驗後
抓到兩個新 MAJOR 與數個 MINOR。**兩個 MAJOR 都是第二輪修正自己引入或沒關掉的**，
這點值得記下來：修正的旁邊就是下一個缺陷的所在地。

### MAJOR-A — 孤兒鎖盲區：`SEARCH_PROCESS` 非 None 但子程序已死 → 鎖永遠不放

第二輪的 `reap_orphan_search_lock()` 判準是「持有鎖但 `SEARCH_PROCESS is None`」。
它漏掉了鏡像的另一半：**`SEARCH_PROCESS` 有值，但那個子程序已經死了**。

那個狀態下兩個回收者互相推讓：

| 函式 | 放棄的理由 |
|---|---|
| `kill_stalled_search()` | `proc.poll()` 不為 `None` → 認為「已經結束了，不關我的事」 |
| `reap_orphan_search_lock()` | `SEARCH_PROCESS is not None` → 認為「有主，不是孤兒」 |

結果是鎖永遠不放。**這正是本案要根除的那種失敗**：timer 每輪 `SKIPPED`、`exit 0`、
日誌一切正常，而全機掃描無限期停擺。

**怎麼進到那個狀態**（不是理論）：`_read_search_output()` 是唯一會呼叫
`_release_search_lock(proc)` 的地方，而它讀的是**爬蟲抓回來的網頁文字**。Popen 的
`errors=` 若為預設的 strict，任何無法以 locale 解碼的位元組都會讓該行拋
`UnicodeDecodeError`、reader thread 當場死亡，永遠走不到釋放。若子行程恰好在那一刻
自行結束，就精準落進這個盲區。

**修法（兩層，缺一不可）：**
1. `_read_search_output()` 的讀取迴圈包 `try/finally`，`_release_search_lock(proc)`
   移到 `finally`。`_release_search_lock` 有 owner 檢查，重複呼叫是安全 no-op。
2. `Popen(..., errors="replace")` —— 寧可看到一個 U+FFFD，也不要一條會靜默停掉
   全機掃描的路徑。（`_follow_live_log` 讀 live log 本來就用 `errors="replace"`，這只是統一。）
3. `reap_orphan_search_lock()` 的判準改為「**子程序是否還活著**」
   （`proc.poll() is None`），而非「欄位是否為 None」。有主的鎖不碰，無主的鎖才收。

### MAJOR-B — `kill_stalled_external()` 會殺無關行程，且會回報假成功

這一項有兩個獨立的傷害，性質不同，所以分開記。

**(1) 拿可能已被回收的 PID 去 SIGKILL。** `search_state.json` 是【上一輪留下的】，
`kill_stalled_external()` 的 gating 卻連 `phase == "finished"` 的 state 也照讀。裡面的
pid 早就可能被系統回收給別的行程 —— 拿它直接送不可逆的 `SIGKILL` 就是隨機殺掉一棵
無關的行程樹。**審查以 `WTERMSIG=9` 實測證實**：受害者是一個與 jobspy 完全無關的
`sleep 300`。這台又是會 suspend 的 VM，PID 回收比一般機器快；專案自己的 MINOR-10
測試也用 `flock -c 'sleep'` 製造過 stray holder，所以這條路徑不是純理論。

**修法（兩層防禦）：**
- **身分閘門**：`state.get("phase") != "running"` → 直接 return。state 自稱已結束時，
  還持有鎖的那個人一定不是我們的掃描（是 stray holder），而 state 裡的 pid 是遺物。
  這裡刻意「寧可不殺」：誤殺是**無界**的傷害（可能殺掉別的服務），而誤殺的替代方案
  只是少一次終止機會。
  phase 停在 `running` 的正常卡死情境（wrapper 被 SIGKILL → EXIT trap 不執行 →
  state 永遠停在 running）不受影響，照樣會被終止。

  > ⚠️ **2026-09-19 第四輪審查更正：「代價有界」的說法只對一半，不能當成通則。**
  > 這裡原本寫「不殺的代價有界 —— 真正卡死的掃描仍會被 `TimeoutStartSec=5h` 收掉」。
  > 審查實測指出這在 **stray holder** 情境下不成立：`trigger=manual` 的手動掃描
  > 根本沒有對應的 unit，5 小時逾時不適用；而 stray holder（例如有人
  > `flock logs/jobscan.lock -c 'sleep'`）持鎖時 `run_scan.sh` 印 `SKIPPED`、**exit 0**、
  > 且刻意不覆寫 state —— 於是 state 停在上一輪的 `finished` → 閘門直接 return →
  > **沒有任何機制會放掉那把鎖**。這條路徑的卡死是無界的。
  >
  > **這不是本輪引進的退步**：修正前也殺不到真正的 holder（它拿的是 state 裡已被
  > 回收或已死的舊 pid，只會殺錯人並留下假成功），所以是「本來就有的洞 + 說法不精確」。
  > **已知且尚未修復。** 真正的解法是照本專案自己的原則——「鎖由誰持有是唯一可信的
  > 事實」——用 `/proc/*/fd` 反查誰開著 `JOBSCAN_LOCK`，再對那個 pid 驗 cmdline，
  > 而不是信任 state 裡的 pid。同一招也能順帶解掉 cmdline 比對的偽陽性問題。
  > 觸發前提是**人為**佔用（爬蟲本身沒有 subprocess/multiprocessing，不會留下
  > 漏繼承 fd 9 的孫行程 —— 這點審查已獨立確認），所以嚴重度不是 CRITICAL。
- **cmdline 身分檢查**：即使 state 自稱 running，pid 也可能在我們讀它之後被回收。
  動手前讀 `/proc/<pid>/cmdline`，確認含 `run_scan.sh` 或 `linkedin_job_search.py`。
  判準用 cmdline 而非「pid 存不存在」——**PID 被回收後同一個號碼會是別的行程，
  只有 cmdline 能區分**。「不確定」一律回 `False`。

**(2) 對 inactive 的 unit 送 `systemctl kill` → 退出碼 0 但什麼也沒殺（本次實測）。**
原系統分支不檢查 unit 狀態就送出 kill，`returncode == 0` 便被當成成功，於是留下
「已終止 jobscan.service」這筆**假成功紀錄**。那比不殺更糟 —— 它會讓下一個追查事故的
人以為停滯偵測正常運作過，正是本案要根除的靜默謊言。
**修法：** 動手前用 `systemctl --user show jobscan.service -p ActiveState --value` 確認，
只接受 `active` / `activating` / `reloading`（**oneshot 執行期間的狀態是 `activating`，
不是 `active`，兩者都要接受**）；否則只留一行 log，改走 PID 路徑（並受上面的 cmdline 檢查）。

### MINOR（本輪一併修正）

| 項目 | 問題 | 修法 |
|---|---|---|
| stale 身分（2.3） | `_external_begin()` 照抄上一輪 `finished` state 的 run_id/trigger/pid。生產日誌實證：27 分鐘前就結束的那一輪被重新「偵測到」一次，連 `exit=0` 都是從舊 state 抄的；那個 stale pid 正是 (1) 的燃料 | 只採用 `phase == "running"` 的身分；否則合成 `trigger="unknown"` / `run_id=None`。真實身分由 `_external_pump()` 在 state 轉為 running 後補認（`run_scan.sh` 是「先取鎖、才寫 state」，那個微秒級窗口本來就設計成由 pump 接手，不是新機制） |
| 生命週期訊息無時戳 | 規劃階段的「掃描結束後 ≤15 秒換檔」判準**在當時的日誌格式下根本不可量測** —— 只能改用 API 輪詢另外量 | 新增 `_log(msg)` 統一輸出 `[HH:MM:SS]` 前綴，23 處 `print` 改用；`run_scan.sh` 同步加 `log()`/`logerr()` |
| 註解寫死行號 | 註解指涉的行號會隨每次編輯飄移，讀者照著找會找到無關的程式碼 | 改為描述函式名／行為，不寫行號 |
| `run_scan.sh` 測試污染生產 state | `JOBSCAN_SCRIPT` 鉤子跑的是同一條生產路徑，測一次就覆寫一次生產的 `search_state.json` 與 `search_current.log`（審查兩次踩到，當時只是手工還原） | `LOCK`/`LIVE`/`STATE` 改為可用環境變數覆寫，**預設值完全不變**；理由不是彈性，是測試隔離 |
| `deploy/README.md` 無測試覆蓋聲明（2.7） | — | 補上「測試覆蓋」專節（含誠實的未覆蓋清單） |
| `DECISIONS.md` 數字錯誤（1.3） | 上一個條目寫「3 項 FAIL」 | 審查實測為 **5 項**，已更正並加註 |

### 變異測試（修正的自我驗證）

每一項修正都做了變異實驗：**把修正改回去，確認測試會抓到**。沒有 FAIL 的修正等於
沒有回歸保護。五個變異全數被捕捉：

| 變異 | 破壞的修正 | 結果 |
|---|---|---|
| mA1 | reaper 判準改回 `SEARCH_PROCESS is not None` | 1 FAIL（正是那個盲區） |
| mA2 | `try/finally` → 只在成功時釋放 | 1 FAIL，`SEARCH_LOCK_FD=3`（鎖洩漏） |
| mB1 | 移除 phase 閘門 | 2 FAIL，變異版**真的送出** `systemctl kill --signal=SIGKILL` 並回報 `'jobscan.service (run_id=stale_000000)'` |
| mB2 | 移除 cmdline 檢查 | 1 FAIL，殺掉無辜的旁觀者 `PID 183293` |
| mB3 | 移除 ActiveState 前置檢查 | 1 FAIL，回報假成功 |

> mB1 第一次只讓 F3 失敗，而 F3 本身是 flaky 的（見下）—— 這等於 phase 閘門**沒有
> 獨立覆蓋**。補了 F4c（mock `subprocess.run` 並斷言 `syscalls == []`）之後，mB1 才
> 穩定產生 2 個 FAIL。
>
> F3 的 flaky 原因：原本用 `sleep 600 linkedin_job_search.py` 製造誘餌，但
> **GNU `sleep` 只接受數值參數，非數值會立刻報錯退出**，誘餌當場死亡。改用
> `bash -c 'exec -a linkedin_job_search.py sleep 600'` 才是有效的偽造 cmdline。

### 未涵蓋（誠實聲明）

`run_scan.sh` 的 bash 端仍然**零自動化測試**；`kill_stalled_external()` 的實際
動手路徑（會殺真行程）測試刻意不碰。詳見本條目上方第二輪條目的說明。

## 2026-09-19 — 掃描鎖的三個缺陷與一個已知限制（第二輪審查）

第二輪 Senior Reviewer 對 `d1c60c8` 判定 `[REVIEW_REJECTED]`，確認 CRITICAL-1 /
MAJOR-1 / MAJOR-2 / MINOR-1 / MINOR-8 五項修正到位，但抓到一個新的 MAJOR-3。
三個缺陷的共同教訓：**這條路徑的失敗都是靜默的**（不拋例外、`exit 0`、日誌正常），
代價卻是全機掃描停擺，所以每一項都補了回歸測試。

### MAJOR-3 — `start_search()` 的 Popen 失敗會洩漏全機掃描鎖（無限期）

`start_search()` 先取鎖才 `subprocess.Popen()`，而 Popen 沒有任何 `try/except`。
失敗時 `SEARCH_LOCK_OWNER` 尚未設定、`SEARCH_PROCESS` 仍是 `None`，fd 永遠不關：

- `kill_stalled_search()` → `SEARCH_PROCESS is None`，直接 return
- `kill_stalled_external()` → `_we_hold_scan_lock()` 早退（鎖確實在我們手上）
- 下次 `start_search()` → 對同檔開第二個 fd 取 flock 仍衝突，**永遠**回
  `"A scan is already running (systemd timer or manual run)"`
- timer 每輪等 20 秒後 `SKIPPED`、`exit 0`、日誌一切正常 → **無限期**資料缺口

觸發路徑：爬蟲檔被改名／刪除（`run_scan.sh` 有 `[ -f "$SCRIPT" ]` 檢查，看板沒有），
或 VM 在 suspend/resume 後 fork/exec 失敗（EAGAIN/ENOMEM）—— 這台正是會 suspend 的 VM。
**修法：** `try/except` 包住 Popen，失敗時 `_release_search_lock()` 並回傳明確錯誤；
狀態欄位（`SEARCH_PROCESS` / `SEARCH_START_TIME` / …）改到 Popen 成功之後才更新，
避免 UI 顯示一個根本沒開始的掃描。

**外加自癒（`reap_orphan_search_lock()`）：** watchdog 每輪檢查「持有鎖但
`SEARCH_PROCESS is None`」是否持續超過 `ORPHAN_LOCK_GRACE`（60 秒）。正常情況下
這個狀態只存在於「取鎖 → Popen」之間的微秒級窗口，所以 60 秒極寬鬆；刻意取寬是因為
**誤放鎖的代價（兩套爬蟲並發，重演 2026-08-13）比多等一分鐘高**。保守之處：只在
`SEARCH_PROCESS is None` 時動手，子程序還在（或 reader thread 仍在收尾）一律不碰。

> ⚠️ **2026-09-19 更正（第三輪審查）：上一段的判準本身就是缺陷。** 「只在
> `SEARCH_PROCESS is None` 時動手」漏掉了另一半：`SEARCH_PROCESS` **非 None 但子程序
> 已經死了**（reader thread 解碼失敗身亡、或子在釋放前自行結束）。那個狀態下
> `kill_stalled_search()` 看 `poll()` 不為 None 而放棄、`reap_orphan_search_lock()`
> 看 `SEARCH_PROCESS` 不為 None 而放棄 —— **兩邊都不管，鎖永遠不放**。
> 現行實作已改為判斷「**子程序是否還活著**」（`proc.poll() is None`）而非
> 「欄位是否為 None」。詳見下方第三輪條目 MAJOR-A。

### MINOR-11 — `kill_stalled_external()` 的 TOCTOU（已縮小，未歸零）

判定（未持鎖 + 逾時）與動手（SIGKILL）之間隔著讀 `search_state.json` 與組字串。
若看板恰在此窗口取得掃描鎖，就會拿前一個外部掃描的 `run_id`/`trigger` 去殺一個
已結束的目標，最壞是 PID 已被回收而殺到無關行程樹。
**修法：** 送訊號前重新確認 `_we_hold_scan_lock()`。
**誠實揭露：** 窗口從「無界（含檔案 I/O 與 subprocess 建立）」縮到微秒級，但沒有歸零
—— 要歸零得把鎖一路持有到訊號送出，而 `systemctl` 是阻塞呼叫，那會讓 watchdog
整體卡住、連自己的掃描都放不掉，比原缺陷更糟。**殘餘風險已知且接受。**

### MINOR-9 — 測試不得佔用生產鎖（已修）

`tests/test_scan_lock.py` 原本對真實的 `logs/jobscan.lock` 取鎖。缺陷版路徑上它會
卡死並持續持有**全機鎖約 53 秒** —— 若此時 timer 觸發，真實掃描會白等 20 秒後 SKIP，
也就是**測試本身能製造這次遷移要消滅的失敗**。改指向 `tempfile.mkdtemp()` 的私人鎖檔。
另外測試行程也會啟動 `job_board` 的背景 watchdog，其 `kill_stalled_external()` 會執行
`systemctl --user kill --signal=SIGKILL jobscan.service`，因此在測試中把該函式換成
no-op 護欄（背景執行緒走模組全域），測試要驗證真正的護欄時改呼叫保存下來的原函式。

### MINOR-13 — 已知限制（接受）：持鎖分支不呼叫 `_external_end()`

看板持有掃描鎖時，`_jobscan_watch_tick()` 直接清掉 `_EXTERNAL["active"]` 並 return，
因此 `maybe_adopt_new_results()` 不會被呼叫、`_EXTERNAL["finished"]` 不會更新
（`/api/search/status` 的 `last_finished` 可能顯示較舊的一輪）。
**實質無害**：前端在 running→idle 轉移時會走 `/api/reload` → `find_latest_kanban()`
載入最新檔，所以看板仍會換到新結果。殘留影響僅止於那個欄位的顯示。
**刻意不修**：在持鎖分支補呼叫 `_external_end()` 會讓「自己的掃描結束」與
「外部掃描結束」兩條路徑的語意糾纏在一起，換取的好處只是一個顯示欄位。

### 未涵蓋（誠實聲明）

自動化測試**只有** `tests/test_scan_lock.py`（**39 項**，含 MAJOR-3 與孤兒鎖自癒），
其餘仍為手動驗證。該測試以變異實驗自我驗證：把 `SEARCH_LOCK` 換回
`threading.Lock()` → 5 項 FAIL 並卡死；拿掉 `except` 區塊的 `_release_search_lock()`
→ **5 項** FAIL（含重現 `"A scan is already running"`）。
> ⚠️ 2026-09-19 更正：此處原寫「3 項 FAIL」。第三輪審查獨立重跑變異實驗，實測為
> **5 項**，本行已更正。低估 FAIL 數意味著低估了這條路徑的波及面。
**未覆蓋**：`run_scan.sh` 的重試邏輯（bash 端仍然零測試）、
`kill_stalled_external()` 的實際動手路徑（會殺真行程，測試刻意不碰）。

## 2026-09-19 — 排程主權移交 systemd timer（解決休眠導致整天不掃描）

**問題：** 晚上電腦休眠 → 喚醒後補跑視窗不足 → 整個時段被跳過。
`scheduler_loop()` 的補跑視窗是**硬性 6 小時**（條件為 `(now - target).total_seconds() < 21600`）：

| 情境 | 結果 |
|---|---|
| 23:00 睡 → 09:00 醒 | 06:00 落後 3h < 6h → 補跑 ✅ |
| 23:00 睡 → **12:30 醒** | 06:00 落後 6.5h > 6h → **整天不掃描** ❌ |
| 跨午夜（23:00 睡 → 07:00 醒） | 前一天 22:00 永久遺失 ❌ |

**根因：** 這台是 VMware VM，**跟著宿主機一起 suspend，VM 無法自行醒來**，
所以 timer 的 `WakeSystem=true` 無效。唯一可行的是「醒來後補跑」。

**決策（使用者選定）：** 排程改由 `jobscan.timer` 負責。
- `deploy/jobscan.timer`：`OnCalendar=*-*-* 06:00:00 / 22:00:00 Australia/Melbourne` + **`Persistent=true`**
- `Persistent=true` 正是「補跑」的原生機制，且**沒有**內建排程器那種人為視窗上限
- 內建排程器**停用但保留**（閘門 `JOB_BOARD_INTERNAL_SCHEDULER`，未設＝停用）
- 維持 **user service**（`Linger=yes` 已開，功能等價且不需 sudo；本機 sudo 需密碼）
- `jobscan.service` 用 `TimeoutStartSec=5h` 而非 alpha 的 `0`（無限）：`Type=oneshot`
  一次只能有一個 instance，若卡死又無逾時，unit 會永遠停在 `activating`，
  之後**每次 timer 觸發都被視為「已在跑」而整批吞掉**，而 `list-timers` 仍顯示漂亮的 NEXT
  —— 這正是 2026-09-17 那次 50 小時卡死的同一種病。爬蟲理論上限 48 詞 × 300s = 4h，故 5h 不會誤殺。

**三道鎖（缺一不可，防止雙軌觸發）：**
1. `scheduler_loop()` 開頭閘門 → thread 直接結束（零喚醒、零 log、零 CPU）
2. 啟動時壓平 `.job_board_schedule.json`：`enabled=false`、清 `_fired_today`、`managed_by="systemd-timer"`
3. `POST /api/schedule {"enabled":true}` 被拒並回 `warning`（不變更、不寫檔）

**⚠️ 實測推翻的假設（重要，別再犯）：** 原以為 `enable --now jobscan.timer` 會因為
`Persistent=true` 立刻補跑一次。**實測不會。** systemd 只在
`~/.local/share/systemd/timers/stamp-jobscan.timer` 這個 stamp「存在且比錯過的時段舊」時才補跑；
stamp 不存在時，它把 stamp 設為 now 然後等下一個時段（避免第一次啟用就回溯觸發所有歷史時段）。
stamp 的**內容是空的，時間存在 mtime**。
正確的驗證方式（2026-09-19 實測通過，Stage 5）：

```bash
touch -d '2026-09-19 05:00:00' ~/.local/share/systemd/timers/stamp-jobscan.timer
systemctl --user restart jobscan.timer   # → 立刻補跑，list-timers 的 LAST 變成 now
```

**可逆性：** `systemctl --user edit jobboard` 加 `Environment=JOB_BOARD_INTERNAL_SCHEDULER=1`
後重啟即可救回內建排程器（程式碼從未被刪，只是被閘門擋住）。

## 2026-09-19 — 全機唯一掃描入口 run_scan.sh；flock 成為唯一「掃描狀態來源」

**問題：** 排程移到 systemd 之後，timer 啟動的爬蟲不在 dashboard 的 `SEARCH_PROCESS`
（in-process 全域）裡 → `/api/search/status` 會回報「沒在跑」，跑完 `JOBS` 也不會重載。
**UI 會比遷移前更糟**，所以這不是加分項而是遷移的必要配套。

**決策：** 新增 `run_scan.sh` 作為**全機唯一**爬蟲入口（systemd timer、`run_search.sh`、
看板 Re-Search 按鈕都走它），並讓 **flock 鎖檔本身成為掃描狀態的唯一真相來源**：
- `run_scan.sh`：`exec 9>"logs/jobscan.lock"` + `flock -n 9` 保證不並發（取不到鎖印 SKIPPED 且 exit 0）
- 持有鎖期間維護 `logs/search_state.json`（原子 `mv`）並 `tee` 到 `logs/search_current.log`
- `job_board.py` 的 `_jobscan_watch_loop()`（每 5 秒）讀鎖 + 跟讀日誌 → 回報
  `running / external / trigger / run_id / output`；掃描結束後自動換檔（尊重使用者已選的檔案）

**關鍵性質（最常見的誤解）：** `flock` 綁 **fd / open file description**，不綁檔案存在 →
**結構上不可能有 stale lock**，刪檔或崩潰都不會留下需要清理的殘鎖。
且 python 子程序**繼承 fd 9**，所以 wrapper 被 SIGKILL 時，只要爬蟲還活著鎖就不會鬆手
—— 因此停滯終止必須殺**整棵行程樹**（`_kill_tree()`，深度上限 10）。
刻意**不用** `os.killpg`：手動掃描的 pgid 是使用者的終端機 pgid，殺下去會連終端機一起殺。

**停滯判定：** 沉默超過門檻 → 殺整棵樹。這是針對 2026-09-17 那次 50 小時卡死的對策：
`tls_client` 的 Go 層會 deadlock，讓 python 的 `timeout_seconds` 完全失效。

**已知缺口（刻意接受）：** 看板若在外部掃描期間掛掉，就沒有停滯偵測，
只剩 `jobscan.service` 的 `TimeoutStartSec=5h` 兜底。`Restart=always` 通常 5 秒內救回看板。

## 2026-09-19 — 日誌輪替改用 systemd timer（不用 cron）+ 必須 copytruncate

**問題：** `job_board.log`（157KB / 2618 行，約 2880 行/天）與 `logs/cron_search.log`（1.4MB）
都無輪替、無限成長。

**決策：** `deploy/logrotate.conf` + `jobboard-logrotate.timer`
（每日 04:30 Melbourne，`Persistent=true`，刻意選在 22:00 掃描結束後、06:00 掃描開始前）。

**兩點刻意與 alpha-validator 不同，勿「對齊」：**
1. **不用 cron。** alpha 用 `0 0 * * *` crontab。這台 VM 跟著宿主機 suspend，
   **cron 錯過就是永遠錯過**；systemd timer 的 `Persistent=true` 會在喚醒後補做。
2. **一定有 `copytruncate`。** 兩個日誌都由 systemd 以 `StandardOutput=append:` 持有 fd，
   systemd **不會重開檔**；用預設的 rename 輪替會讓寫入繼續落在已改名的舊 inode
   （幽靈檔），而新的 `job_board.log` 永遠是空的。

**實測驗證（Stage 7）：** 輪替後 inode **不變**（408086，原地截斷）、大小歸零；
對看板發請求 → 新內容落在**新的** `job_board.log`（204 bytes，3 筆 GET）；
舊檔 `job_board.log-2026-09-19` 大小 160097 → 160097 **未成長**（證明無幽靈寫入）；
`grep -P '\x00'` 無 NUL 空洞（證實 `append:` 以 `O_APPEND` 開檔，截斷後寫入落在檔尾）。
`jobboard-logrotate.service` 本身 rc=0 / `ExecMainStatus=0`。

**已知取捨：** `copytruncate` 在「複製完、截斷前」有極短窗口會遺失寫入。
若 logrotate 補跑時正好撞上 06:00/22:00 的掃描，`cron_search.log` 可能少掉幾行逐字稿。可接受。

**注意：** `logs/search_current.log` **刻意排除**在輪替之外 —— 它由 `run_scan.sh`
每輪 `: >` 截斷，本來就是有界的，且是看板即時跟讀的來源，輪替只會讓 follower 白做一次 reset。

## 2026-09-13 — 完整遷移到 Oracle VPS，以 SSH tunnel 存取 dashboard

> ⚠️ **2026-09-19 更正：本機沒有退役，且本機重新成為主要執行環境。**
> 使用者需求變更為「我的本地還是要繼續跑」。另外探索時確認：VPS 的機房 IP 會被
> Cloudflare 站點（Jora）擋掉，本機可抓 —— 這使「全部搬 VPS」的代價比當初評估的更高。
> 因此本機 `jobboard.service` 明確綁 `JOB_BOARD_HOST=127.0.0.1`（純本機看板、不對外），
> 遠端存取改用 `ssh -L 5000:127.0.0.1:5000 <host>`。
> **本條目其餘內容仍然有效**：editable install 那個 P0（PyPI 版缺 `JORA` 成員導致四來源
> 全滅且 exit code 仍為 0）、SSH tunnel 而非公開埠的決策，都繼續適用。



**問題：** dashboard 只綁本機 LAN（`http://192.168.44.128:5000`），離開家裡網段就無法開啟。

**兩個決策（使用者選定）：**
1. **全部搬 VPS** —— 爬蟲與 dashboard 都在 VPS 執行，本機退役。**接受 Jora 歸零**。
2. **SSH tunnel 存取** —— 不公開任何埠、不改 iptables / OCI security list、不加認證程式碼。
   VPS 的 unit 明確設 `JOB_BOARD_HOST=127.0.0.1`（僅縱深防禦）。

**遷移中發現的 P0（若不處理會靜默全滅）：** `pip install python-jobspy` 從 PyPI 裝到的是
上游 1.1.82，**沒有 `JORA` 成員**。而 `get_site_type()` 用 list comprehension 建 `site_types`
（`jobspy/__init__.py:82-86`），**一個未知名稱就讓四個來源全部中止**，且在任何 HTTP 請求
之前就爆掉；例外被 `linkedin_job_search.py:787` 的 broad `except` 吞掉 → 印
`❌ No results found` → **exit code 仍是 0** → dashboard 顯示 `✅ Search completed!`。
修正：改用 **editable install**（`pip install -e ~/jobspy`），與本機的 `python_jobspy.pth` 一致。
已實測：`import jobspy` 解析到 `/home/ubuntu/jobspy/jobspy/__init__.py`、
site-packages **沒有** shadow `jobspy/` 目錄、`Site` 有 10 個成員（上游 9 個，無 jora）。

**關鍵量測 —— 機房 IP 沒有被節流（本次最重要的結論）：**
同一組 6 個搜尋詞，本機 vs VPS 逐來源比對：

| 來源 | 本機 | VPS |
|------|------|-----|
| linkedin | 11 | **11** |
| indeed | 5 | 6 |
| seek | 2 | 1 |
| jora | 11 | **0** |

LinkedIn 完全相同，Indeed/Seek 在正常跳動範圍內。**唯一的差異就是 Jora**，
而那是已知且已被接受的 Cloudflare 403。原計畫把「機房 IP 對 LinkedIn/Indeed 的
持續封鎖」列為頭號未驗證風險 —— 實測結果推翻了它。

**既有缺陷（本次發現並已修正，`c903c5b`）：** `scrape_jobs()` 的 `verbose` 預設值是 **0**
（`jobspy/__init__.py:52`），而應用端從不覆寫它。`verbose=0` → `set_logger_level(0)`
→ **ERROR 等級**，因此**所有 WARNING / INFO 都被丟棄**。後果：Jora 的 403 警告
（`jobspy/jora/__init__.py:277`）永遠不會出現在日誌中，使用者只會看到「0 筆」而無任何解釋。
這正是先前 P0「靜默失敗」的同一個坑，違反本專案「不允許靜默失敗」原則。
（附帶：`jobspy/util.py:140` 的 docstring 寫 `default=2`，與實際預設值 0 不符 —— 未修，
屬上游文件問題。）

**修正：** 呼叫端明確傳 `verbose=SCRAPE_VERBOSE`（新常數，預設 `1` = WARNING），
並在註解中記錄 0/1/2 三級語意，避免日後被當成雜訊「清理」掉。
選 1 而非 2：WARNING 已能暴露來源失效，又不會產生每詞的 INFO 洗版。

**實測驗證（在 Jora 被擋的 VPS 上跑單詞）：** 修正後日誌確實出現
`WARNING - JobSpy:Jora - Jora: HTTP 403 (not retryable) for https://au.jora.com/...`，
**每詞恰好一行**，且無其他 jobspy 輸出。修正前同一情境是完全靜默的。

**驗證（無自動化測試，全為手動程序）：**
- 服務 `active` + `enabled`，`Linger=yes`，**實際重開機後自動起**（boot id 已變、
  uptime 0 分鐘、未經登入即啟動）
- 只聽 `127.0.0.1:5000`，對外**連不上**（iptables 只放行 22，已實測拒絕）
- tunnel 進來的頁面與 VPS loopback 直取**位元組完全相同**（29,220 bytes）
- 歷史 209 次執行、`.job_statuses.json` 32 筆（7 Rejected / 24 Applied / 1 Interview）
  全部跨重開機倖存；status overlay 逐筆比對 0 筆不符
- 排程 `enabled: true`、`next_run 2026-09-13 22:00`、`times [06:00, 22:00]`

**存取方式：**
```bash
ssh -N -L 5000:127.0.0.1:5000 ubuntu@129.150.42.124
```
然後瀏覽器開 `http://localhost:5000`。tunnel 斷了重跑即可。

**本機處置：** 服務 `stop` + `disable`（**只停用、不刪除**），程式與資料原封不動，
必要時 `systemctl --user enable --now jobboard` 一行復原。本機埠 5000 已釋出給 tunnel 用。

**生效：** job_board.py 每次搜尋 spawn 新 subprocess，無需重啟。

## 2026-09-13 — 移除失效的 Google 來源、新增 Jora（au.jora.com）

**問題：** Google 來源每次回傳 0 筆，卻仍在 48 個搜尋詞的迴圈中被呼叫 48 次。

**根因：** Google 已改為 JS-required shell（三種 URL 變體皆帶 `enablejs=True`），
兩個解析錨點（`520084652` 神奇鍵、`jsname="Yust4d"` 游標 div）出現次數皆為 **0**。
PyPI 最新版已是 1.1.82，無上游修復可用，故此來源無法修復。

**決策：** 移除 Google，新增 Jora 作為第四個來源。爬蟲維持**在本機執行** ——
Oracle VPS 為新加坡機房 IP（AS31898），Jora 的 Cloudflare 對其回 **403
"Just a moment..."**，而本機住宅 IP（Vodafone AU）正常。

**Jora 的三個實測陷阱（皆已納入實作）：**
1. `tls_client` 回應**沒有** `raise_for_status()`。照抄 Seek 會拋 `AttributeError`
   並被 broad `except` 吞掉，結果是 0 筆加一行 debug 日誌 —— 看起來像被封鎖，
   實則是程式碼 bug。也不能用 `response.ok`（它接受 200–399，而 tls_client
   預設不跟隨重導向）。
2. `_AGE_RE` 中 `mo` 必須排在 `[hdw]` 之前。寫成 `([hdwmo])` 會讓 `"5mo ago"`
   匹配失敗並 fallback 成「今天」，使 5 個月前的職缺**通過** 7 天過濾。
3. 標題會被 Jora 汙染：`h2.job-title` 內含 desktop/mobile 兩個 `<a>`（標題重複），
   且徽章文字直接黏在 anchor 文字節點尾端（`"...EngineerNew"`）。注意卡片上的
   `data-impression-badge-status` **不可**作為判斷依據 —— 120 張卡片全帶
   `NEW:NEW`，但只有 4 張標題真的被汙染。

**設計取捨：** Jora 忽略所有日期參數（`&d=`/`&date=`/`&posted=` 實測無效），
故 `hours_old` 改在爬蟲端以 `.job-listed-date` 過濾，且刻意排在抓詳情頁**之前**
（480 → 約 163 筆請求）。不建跨詞描述快取：日期過濾已先行，快取僅省約 2.5 分鐘，
不值得在常駐服務引入無自然失效點的行程級全域狀態。

**已知限制（刻意接受，非缺陷）：** Jora 做模糊寬鬆匹配，對*任何*查詢都回滿
15 筆（`ORAN engineer` 會回傳風電工程師），且搜尋卡片**完全沒有薪資欄位**。
噪音交由下游 `MIN_SCORE=5` 過濾。每詞僅產出約 5–12 筆（低於 `results_wanted=20`），
由消費端跨 48 詞去重吸收。

**驗證中發現並修正的 bug（URL 未正規化導致去重失效）：** 首次端到端跑完後，
Jora 貢獻 134/182 筆（73.6%），但**只有 32 個唯一職稱、24 間唯一公司** ——
單一職缺（Andromeda Robotics 的 Embedded Linux Engineer）重複出現 **22 次**。
根因：Jora 的 href 帶著**每次搜尋都不同**的追蹤參數（`sol_key` / `tk` / `sq` / `sr`），
故同一筆職缺在不同搜尋詞下產生**不同的 URL 字串**，消費端
`drop_duplicates(subset=["job_url"], keep="first")`（linkedin_job_search.py:801）
因此完全失效。修正：新增 `_canonical_url()`，以 `urlsplit`/`urlunsplit` 剝除查詢字串
與 fragment，只保留穩定的 `/job/<slug>-<id>`。已實測剝除後仍回 HTTP 200 且描述完整
（10043 vs 10078 字，差異屬正常浮動）。修正後 2 詞實測：21 筆 → 17 唯一 URL，
0 筆帶查詢字串，跨詞重複正確塌縮。

**已知殘留（非本專案可控）：** Jora 自身會把同一則廣告以**不同 job id** 重複上架
（實測 Hastha Solutions 的 Broadband Devices Tester 有 `278deab4…` 與 `55042871…`
兩個獨立 id、描述長度略異）。這類重複在 `job_url` 層級無法辨識，需以
(title, company) 去重才能收斂 —— 屬消費端行為，本次不變更。

**驗證：** 無自動化測試（專案無 `tests/`），全為手動程序。`_parse_age` 10 案例
（含 `5mo` 陷阱回歸）、`_dedupe_title`、`_strip_badge` 7 案例、`_normalise_location`
皆通過；4 詞 34 筆**超 7 天者 0 筆**；詳情頁 10/10 成功（1981–11094 字）；
四來源整合確認 google 已移除、jora 進入；下游三個引擎處理 Jora 資料列 10/10 零錯誤；
平均 7.1 秒/詞。URL 正規化修正後複驗：語法、`self._*` 參照完整性、無 >88 字元行、
`import jobspy` 皆通過。

**生效：** job_board.py 每次搜尋 spawn 新 subprocess，無需重啟，下一次排程起生效。

## 2026-08-19 — dashboard 改以 systemd user service 常駐

> ⚠️ **2026-09-19 更正（兩處）：**
> 1. `run.sh` / `stop.sh` 已改寫成 `systemctl --user restart|stop jobboard.service` 的薄殼，
>    **可以正常使用**。舊版 `stop.sh` 用 `pgrep` + `kill`，會被 `Restart=always` 在 5 秒後
>    復活 —— 它從來沒有真正停掉過服務（實測：舊版停不掉，新薄殼停得住）。
>    新版**刻意不提供 nohup fallback**：`>` 會用 O_TRUNC 清空 systemd 正在 append 的日誌，
>    且會與 `Restart=always` 搶 5000 埠造成 EADDRINUSE flap。
> 2. 本條目最後寫的「排程配置（06:00 / 22:00 + 6h 補觸發）不變」**已被推翻**，
>    見上方 2026-09-19 條目。



**問題：** 2026-08-13 決策後 dashboard（job_board.py）為唯一排程來源，
但僅靠 `./run.sh`（nohup）啟動，重開機或程序死掉後不會自動恢復
（本次檢查時已無執行，job_board.log 停在當日 07:40）。

**決策：** 改用 systemd user service 常駐（`~/.config/systemd/user/jobboard.service`）。
- `Restart=always`（崩潰 5 秒後自動重啟）
- `WantedBy=default.target` + `Linger=yes` → 開機即啟動、無需登入
- 日誌沿用 `job_board.log`（append）
- 排程配置（06:00 / 22:00 + 6h 補觸發）不變

**管理指令：**
- 查看：`systemctl --user status jobboard`
- 重啟：`systemctl --user restart jobboard`
- 停止：`systemctl --user stop jobboard`
- 日誌：`tail -f job_board.log`

**後續：** `run.sh` / `stop.sh` 保留但僅供手動除錯用；啟用服務後勿再使用。

## 2026-08-13 — 排程改為 dashboard 唯一來源

> ⚠️ **2026-09-19 更正：排程主權已移交 systemd timer，「dashboard 為唯一排程來源」不再成立。**
> 本條目的**核心貢獻仍然成立且更徹底** —— 它要解決的是「多重觸發來源互不可見」，
> 現在由 `run_scan.sh` 的 flock 從結構上根絕（全機唯一入口，見 2026-09-19 條目）。
> 但「由 dashboard 排程」這件事本身已被取代：dashboard 的內建排程器已停用（三道鎖）。
> 同時 `deploy/jobscan-morning.service/.timer` 與 `morning_catchup.sh` 已於 2026-09-19
> **`git rm`** —— 兩者都是被取代，不是因為逾時設定。前者缺 `TimeoutStartSec`，
> 而 `Type=oneshot` 未設此值時 systemd 給的是**無限**（2026-09-19 實測：apt-daily /
> fstrim / man-db / systemd-update-utmp 皆為 `TimeoutStartUSec=infinity`；系統的
> `DefaultTimeoutStartUSec=1min30s` 不適用於 oneshot。本條目初稿誤寫為「90 秒預設」，
> 已更正），所以它一旦卡死會永遠停在 `activating` 並吞掉之後所有觸發 —— 但這只是
> 加分項，刪除的主因是它已被 `jobscan.timer` 取代；
> 後者做的事已被 `Persistent=true` 取代，留著只會在有人加 cron 時重演本條目記載的並發事故。



**問題：** 搜尋排程有 3 個實際來源，造成重複觸發：
1. crontab `0 22 * * *` 直接執行 `linkedin_job_search.py`
2. crontab `*/15 6-12 * * *` 執行 `morning_catchup.sh`（補跑早上）
3. dashboard（job_board.py）內建 scheduler（06:00 / 22:00，含 6h 晚開機補觸發視窗）

證據：2026-08-13 早上兩套系統並發重複搜尋（08:45 catchup + 08:49 dashboard 補觸發，各產一份結果檔）。
兩者的防呆互不可見（`start_search()` 只檢查自身 SEARCH_PROCESS；catchup 的結果檔檢查在 dashboard 觸發前執行則攔不住）。

`deploy/` 下的 systemd units（jobscan-morning.timer/service）**從未安裝**（not-found），僅為範本。

**決策：** dashboard 為唯一排程來源。
- 從 crontab 移除 jobspy 2 行（備份：`/tmp/crontab_backup_20260813.txt`）
- 保留 dashboard scheduler 06:00 / 22:00 + 內建補觸發
- 前提：dashboard 需常駐執行（`./run.sh`）

**後續：** `morning_catchup.sh` 與 `deploy/` 已無作用但仍保留，可確認後刪除。

## 2026-08-17 — Security Clearance 分類器強化（ITAR / work rights / 複數 / 反向措辭）

**問題：** Ascent Vision「Embedded Software Engineer - Linux」職缺描述含
「full, permanent work rights」「ITAR」「able to obtain National Police and other
related security clearances」，卻被分類為「✅ No Clearance Required」。

**根因（linkedin_job_search.py）：**
1. Pattern 漏洞：
   - `\bclearance\b` 不匹配複數「clearances」
   - 「be able to obtain」未覆蓋（僅 eligible/ability to obtain）
   - ITAR / International Traffic in Arms Regulations 無 pattern
   - 「full, permanent work rights」無 pattern
2. 順帶發現的反向 bug：「No security clearance required」等反向措辭會被誤擋
   （positive loop 先於 negative check，且 negative pattern 缺「no … required」形式）

**修正：**
- SECURITY_CLEARANCE_PATTERNS：複數 `clearances?`、新增
  「be/are able to obtain」、ITAR x2、tempered-dot 防跨越 no/not、
  319 加 `(?<!no )(?<!not )` 防護
- CITIZENSHIP_REQUIRED_PATTERNS：新增「must have full, permanent work rights」
- check_security_clearance()：新增 ITAR / WORK RIGHTS level 分類
- negative_patterns：新增「no (security) clearance(s) required」

**驗證：** 13 項測試全過（該職缺 → blocked=ITAR Export Control；
反向措辭不誤擋；NV1/Baseline 回歸仍擋）。

**生效：** job_board.py 每次搜尋 spawn 新 subprocess，無需重啟，
下一次排程（22:00）起自動生效。
