# DECISIONS.md

重要決策紀錄 — 依專案工作流程要求更新。

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

### 第五輪變異測試結果（`tests/mutate.py`，13 個變異，12 被逮、1 已知逃脫）

變異工具從 `/tmp` 搬進版控（`tests/mutate.py`），**上面每一個「被逮」的宣稱
都可以用一行指令重跑**。

| 變異 | 內容 | 結果 |
|---|---|---|
| M1 | 孤兒回收判準改回 `proc is not None` | 55P / 1F ✅ |
| M2 | 拿掉 `_read_search_output` 的 try/finally | 55P / 1F ✅ |
| M3 | 拿掉 `Popen(errors="replace")` | 55P / 1F ✅ |
| M4 | 拿掉 `kill_stalled_external` 的 phase 閘門 | 54P / 2F ✅ |
| M5 | 拿掉 cmdline 身分檢查 | 55P / 1F ✅ |
| M6 | 拿掉 systemd 分支的 ActiveState 前置檢查 | 54P / 2F ✅ |
| M7 | 拿掉 `_external_begin` 身分閘門 | 54P / 2F ✅ |
| M8 | `kill_stalled_external` 整個 no-op | 51P / 5F ✅ |
| M9 | 拿掉 TOCTOU 重檢 | 55P / 1F ✅ |
| M10 | `_pid_is_our_scan` 退回子字串比對 | 55P / 1F ✅ |
| M11 | `_out` 退回 `print()`（修正前行為、無鎖） | 53P / 3F ✅ |
| M12 | 保留鎖但用 `print()`（兩次 write） | 53P / 3F ✅ |
| M13 | 拿掉鎖、保留單次 write | 56P / 0F ⚠️ **已知逃脫** |

> ⚠️ **這張表的 PASS/FAIL 數會隨測試項數變動**（測試從 55 加到 56 之後，
> M1 就從 54P/1F 變成 55P/1F）。**權威來源是 `tests/mutate.py` 的實跑輸出**，
> 這張表只是某一次的快照；會變的數字不是重點，**判定欄（被逮／逃脫）才是**。
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
