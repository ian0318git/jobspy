# deploy/ — systemd 部署說明

> 2026-09-19 起，排程主權在 **systemd user timer**，不在 dashboard 的內建排程器。
> 決策理由與實測證據見 `../DECISIONS.md` 的 2026-09-19 三則條目。

## 檔案清單

| 檔案 | 角色 |
|---|---|
| `jobboard.service` | 常駐看板（`job_board.py`）。`Restart=always`，`WantedBy=default.target` |
| `jobscan.service` | 跑一次爬蟲（`run_scan.sh`）。`Type=oneshot`，**沒有 `[Install]`** |
| `jobscan.timer` | 每天 06:00 / 22:00（Melbourne）觸發 `jobscan.service`，`Persistent=true` |
| `jobboard-logrotate.service` | 跑一次 `logrotate`。`Type=oneshot`，**沒有 `[Install]`** |
| `jobboard-logrotate.timer` | 每天 04:30（Melbourne）觸發輪替，`Persistent=true` |
| `logrotate.conf` | 輪替規則（`copytruncate` 是必要的，見下） |

`jobscan.service` 與 `jobboard-logrotate.service` **刻意沒有 `[Install]` 區塊**：
讓 `systemctl --user enable` 在結構上失敗，就不可能出現「開機即掃描」這種意外。
它們只該被各自的 timer 拉動。

## 安裝

```bash
cd /home/ian/github-project/jobspy
install -m 644 deploy/jobboard.service deploy/jobscan.service deploy/jobscan.timer \
                deploy/jobboard-logrotate.service deploy/jobboard-logrotate.timer \
                ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jobscan.timer jobboard-logrotate.timer
systemctl --user restart jobboard.service
```

前置條件：`loginctl show-user "$USER" -p Linger` 必須是 `Linger=yes`
（否則登出後 user service 會被收掉）。本機已開啟。

`deploy/logrotate.conf` **不需要**複製到別處 —— unit 內用絕對路徑指向 repo 裡的這一份。

驗證：

```bash
systemctl --user list-timers --all | grep -E 'jobscan|logrotate'
systemctl --user is-enabled jobscan.timer jobboard-logrotate.timer   # 都要 enabled
systemctl --user is-active  jobboard.service                          # active
curl -s http://127.0.0.1:5000/api/search/status | python3 -m json.tool
```

`/api/search/status` 的 `timer.installed` 必須是 `true`。
若為 `false`，看板會在標題列顯示「⚠️ timer 未安裝」—— 這是刻意的靜默失敗偵測器。

---

## ⚠️ 兩個絕對不要改的地方

### 1. `jobscan.timer` / `jobboard-logrotate.timer` 不要加 `Requires=<對應的>.service`

`[Unit]` 區塊的 `Requires=` **會在 timer 啟動的當下立刻把 service 拉起來一次**，
不是「觸發時才拉」。alpha-validator 的 journal 是鐵證：

```
23:09:57  Started alpha-pre-market.timer
23:10:04  Starting alpha-pre-market.service      ← 只差 7 秒，這是開機造成的
```

alpha 靠 `wrapper.sh` 的 `.done` marker 把那次拉動變成 1 秒 no-op，**jobspy 沒有那層保護**
—— 加了 `Requires=` 就會變成「每次開機多跑一輪完整掃描」，而且不會有任何錯誤訊息。

**偵測方式：** 開機後 1 分鐘內 `search_results/` 多出一個新檔。

### 2. `logrotate.conf` 不要拿掉 `copytruncate`

`job_board.log` 與 `logs/cron_search.log` 都由 systemd 以 `StandardOutput=append:` 持有 fd，
**systemd 不會重開檔**。用 logrotate 預設的 rename 方式輪替，寫入會繼續落在已改名的舊 inode
（幽靈檔），而新的 `job_board.log` 永遠是空的 —— 而且 `systemctl status` 一切正常，看不出來。

實測證據（2026-09-19 Stage 7）：輪替後 inode 不變、新寫入落在新檔、舊檔大小未成長、
無 NUL 空洞。

### 3.（順帶）不要為了「跟 alpha 對齊」把 logrotate 改回 crontab

alpha 用 `0 0 * * *` cron。這台 VM 跟著宿主機 suspend，**cron 錯過就是永遠錯過**，
systemd timer 的 `Persistent=true` 才會在喚醒後補做。理由與 `jobscan.timer` 完全相同。

---

## 關於 `Persistent=true` 的 stamp 檔（容易誤判）

補跑狀態存在 `~/.local/share/systemd/timers/stamp-<timer>.timer`。
**檔案內容是空的，時間存在 mtime。**

- stamp **不存在**時，systemd 把 stamp 設為 now 然後等下一個時段
  → **首次 `enable --now` 不會回溯補跑**（這點與直覺相反，已實測）
- stamp **存在且比某個已過的時段舊**時，才立刻補跑

要手動驗證補跑（不需改系統時鐘、不需等到排程時刻）：

```bash
touch -d '2026-09-19 05:00:00' ~/.local/share/systemd/timers/stamp-jobscan.timer
systemctl --user restart jobscan.timer
LC_ALL=C systemctl --user list-timers jobscan.timer --all   # LAST 應變成剛剛
```

## 手動操作

```bash
# 立刻跑一次掃描（會與 timer 互斥，取不到鎖就印 SKIPPED 並結束）
./run_search.sh

# 用 systemd 跑一次掃描（--no-block 必要，否則會卡住等 19 分鐘）
systemctl --user start --no-block jobscan.service

# 看掃描逐字稿
tail -f logs/cron_search.log

# 手動輪替日誌（先 dry-run）
/usr/sbin/logrotate -d deploy/logrotate.conf --state logs/logrotate.state
/usr/sbin/logrotate -fv deploy/logrotate.conf --state logs/logrotate.state
```

## 測試覆蓋（誠實聲明）

**這個專案的自動化測試只有一支**：`tests/test_scan_lock.py`（58 項檢查）。
其餘全部是手動驗證 —— 上面各節的「驗證」指令就是手動程序。

```bash
cd /home/ian/github-project/jobspy
.venv/bin/python tests/test_scan_lock.py     # 通過時印「✅ 全數通過」且 exit 0

# 要引用「幾項」時用這個量，不要憑印象寫 —— 這個數字已經腐化過五次（28→39→53→55→56→58）：
.venv/bin/python tests/test_scan_lock.py | grep -c '\[PASS\]'
```

| 區段 | 項數 | 涵蓋 |
|---|---|---|
| A / A2 | 2 + 3 | `SEARCH_LOCK` 必須可重入（CRITICAL-1）、完整 `kill_stalled_search()` 路徑 |
| B | 4 | 過期 owner 不得釋放現任鎖（含 8 執行緒 hammer） |
| C | 5 | 看板持鎖時不得留下凍結的 `active` 狀態 |
| D | 6 | Popen 失敗時必須把鎖還回去（MAJOR-3） |
| E | 5 | 孤兒鎖自癒 |
| F | 17 | 孤兒鎖盲區、誤殺無關行程、幻影掃描、假成功、`cmdline` 身分驗證（第三輪退回） |
| F7 | 7 | 殺戮路徑的**正向**覆蓋：必須開火、且目標真的死掉（第四輪退回） |
| G | 4 | `errors="replace"` 與 TOCTOU 重檢的回歸保護（第四輪退回） |
| H | 3 | 日誌每行必須**一次** `write()` 寫出（第五輪退回，見下） |
| I | 1 | `job_board.py` 不得有 live 的 `print()`（第五輪退回） |
| J | 2 | 啟動時的排程主權轉移：兩個方向都要正確（見「回復到舊制」一節） |

> **H 區的 `PYTHONUNBUFFERED` 前提**：黏行的成因（`print` 拆成兩次 `write()`）
> 只有 stdout **不緩衝**時才會顯現 —— 第五輪審查用有緩衝的檔案實測，`print`
> 竟然 0 黏行。unit 裡的 `Environment=PYTHONUNBUFFERED=1` 是這個缺陷的前提之一。
> **拿掉它、或把日誌改成 `StandardOutput=journal`，測試不會提醒你。**

**測試本身有護欄**（因為它 import `job_board` 會連帶啟動背景 watchdog）：

- 鎖檔指向 `tempfile.mkdtemp()` 的私人檔案 —— **絕不碰生產的 `logs/jobscan.lock`**。
  這一項是第二輪審查的 MINOR-9：原本測試會持有全機鎖約 53 秒，若此時 timer 觸發，
  真實掃描就會白等 20 秒後 SKIP ——**測試本身能製造這次遷移要消滅的失敗**。
- **跳線（tripwire）**：`jb.subprocess.run` 被包了一層，任何 argv 含 `"kill"` 的呼叫
  當場拋 `AssertionError`。合法用途（F7b）本來就會 mock 掉 `subprocess.run`，
  所以**根本走不到跳線** —— 它只在寫錯時響，不需要 opt-in 開關。
- `run_scan.sh` 的 `LOCK`/`LIVE`/`STATE` 可用環境變數覆寫，就是為了讓手動的
  端到端測試**不污染生產的 `search_state.json`**（第三輪審查的 MINOR）。

> ⚠️ **不要以為「把 `kill_stalled_external` 換成 no-op」就是護欄。**
> 第四輪審查實證：測試本文是直接呼叫**先前存下來的** `_REAL_KILL_EXTERNAL()`
> ——換掉模組全域的**名字**擋不住已保存的**函式參照**。當時的後果是測試會對
> **生產的** `jobscan.service` 送出真的 SIGKILL，殺掉使用者正在跑的那一輪。
> 現在靠的是上面的跳線（在最底層擋），不是那個 no-op。
> **任何直接呼叫 `_REAL_KILL_EXTERNAL()` 的新測試都必須 mock `subprocess.run`。**

**以變異實驗自我驗證**：每一項修正都做過「把它改回去，確認測試會 FAIL」。
沒有 FAIL 的修正等於沒有回歸保護。工具已進版控，**上面每一個「被逮」的宣稱
都可以自己重跑**：

```bash
cd /home/ian/github-project/jobspy
.venv/bin/python tests/mutate.py     # 需乾淨的工作區；在 /tmp 隔離副本裡跑
```

目前 **15 個變異、14 個被逮、1 個已知逃脫**（M13，理由見 `../DECISIONS.md`
第四輪條目；`mutate.py` 的 `EXPECTED_ESCAPES` 把「已理解的逃脫」與「沒被發現的
覆蓋缺口」分開回報，只有後者會讓退出碼變 1）。**若你新增修正卻找不到會失敗的
變異，代表那個修正沒有被測試覆蓋。**

> ⚠️ **這個工具不會碰生產目錄。** 第五輪審查抓到：舊版直接改寫 repo 裡的
> `job_board.py`，當時的 13 個變異每個會在磁碟上存在 5–25 秒，而 `jobboard.service`
> 是 `Restart=always` + `MemoryMax=512M` —— 若這段窗口內被 OOM 殺掉而重啟，
> **新行程載入的就是那個變異**（清單裡有 M5／M6／M8，其中 M8 是停滯偵測整個
> no-op）。現在改用 `git archive HEAD` 解到 `/tmp` 的隔離副本，生產檔案從頭到尾
> 不被寫入，結束時以 sha256 驗證。

> **變異表的數字是可重現的。** 這句話在 2026-09-20 之前是**假的** —— J 區有一項
> 會間歇性失敗（見下方第五個坑），同一份程式碼連跑兩次會得到不同的 P/F。
> 修掉之後連跑兩次逐項相同。**若你看到數字漂移，先當成有東西不穩定，不要當成
> 「本來就會這樣」。**

五個踩過的坑，寫在這裡免得重蹈：

- **假變異**：改到註解的變異**不可能改變行為**，所以永遠不會 FAIL、永遠「逃脫」。
  看到「逃脫」先懷疑變異本身。
- **殘留檢查不能用 grep**：「把 `kill_stalled_external` 整個 no-op」的變異是一個
  裸的 `return None`，任何樣式比對都抓不到。現在靠的是「根本不在生產目錄裡跑」，
  比事後檢查殘留更根本。
- **⚠️ 有修正 ≠ 有回歸保護，連寫測試的人自己都會中**：H 區第一版用 8 執行緒對
  `StringIO` 猛寫、斷言沒有黏行 —— 結果把 `_out()` 退回修正前的 `print()`（M11）
  之後**全數通過**。`StringIO` 太快，GIL 在兩次 `write()` 之間幾乎不切換，
  race 逼不出來。修法是**斷言成因而非賭 race**（逐次檢查 `write()` 呼叫），
  決定性、不會間歇性失敗。
- **⚠️ 同一個錯，我在下一輪又犯了一次**：補上的 I 區（AST 掃描）第一版把
  「落在 `_out` span 內」整段豁免，於是 M11 的那個 `print` 也被放行 ——
  **這一項根本沒開火，而測試仍然是綠的**。發現方式是對照失敗項數：
  照理三項該響卻只有兩項。
  **變異測試不只驗證修正，也驗證了驗證本身。** 光看「測試通過」永遠不夠 ——
  唯一能區分的動作是把修正改回去，看它會不會響。
- **⚠️ 綠燈也可能是運氣 —— 要連跑很多次才算數**：J 區（用子行程驗證啟動時的排程
  轉移）20 次裡只有 14 次全綠。我第一次跑了 3 次都過就往下走了。
  根因不是斷言，是**子行程的死法**：`python -c "import job_board"` 會啟動三個
  daemon 執行緒，而 `-c` 一結束主執行緒就進入 interpreter finalization，那些
  執行緒還在寫 stdout → `Fatal Python error: could not acquire lock for
  <_io.BufferedWriter name='<stdout>'> at interpreter shutdown` → **SIGABRT**。
  實測 forward 25 次中 3 次、reverse 25 次中 2 次。
  **修法是子行程結尾用 `os._exit(0)` 跳過 finalization。**
  > 這**不是**生產缺陷，已查證：`systemctl stop` 送 SIGTERM 時 Python 沒裝 handler
  > （`signal.getsignal(SIGTERM)` 回 `0` = `SIG_DFL`），核心直接終止、不跑
  > finalization，所以 journal 從 2026-09-19 至今 0 筆 ABRT。只有在「直譯器
  > **正常結束**」時才會踩到。
  > **但任何這裡新寫的測試只要用子行程 import `job_board`，就必須這樣收尾**，
  > 否則你會得到一個隨機紅的測試，而隨機紅的測試最後會被人加 `|| true` 繞過。

### ⚠️ 未涵蓋（不要以為有測試就安全）

- **`linkedin_job_search.py` 有同一個兩次 syscall 的結構**（56 個 `print`，
  `jobscan.service` 同樣 `PYTHONUNBUFFERED=1`）。今天沒有症狀（`_worker` 不 print，
  `cron_search.log` 用雙時戳啟發式檢查 0 筆），但若爬蟲改多執行緒輸出、或
  `run_scan.sh` 的 echo 與爬蟲的 print 交錯，同一個缺陷會在另一份日誌復發。
  **I 區的 AST 掃描只涵蓋 `job_board.py`。**
- **`run_scan.sh` 的 bash 端：零自動化測試。** 鎖重試、`SKIPPED` 路徑、`LOCK_WAIT`
  驗證、`finish()`／`trap` 的退出碼語意，全部只有手動驗證過。改這支腳本時請照
  上面「手動操作」一節實測。
- **stray holder 造成的卡死**：`trigger=manual` 的手動掃描沒有 unit 可等，
  而 stray holder 持鎖時 state 停在上一輪的 `finished` → phase 閘門直接 return →
  **沒有任何機制會放掉那把鎖**。已知、未修，理由與解法見 `../DECISIONS.md`
  第四輪條目的「已知限制」。
- **排程器「跑完之後」算下一次的那段沒被測到（已知、未修）**：`scheduler_loop()`
  跑完一輪後用 `times[0]`（**未排序**）算下一個時段，而 UI 儲存那條用
  `sorted(times)`。若 `times` 被存成 `["22:00","06:00"]` 且當天時段都已過，
  排程器會算出「今天 22:00」而不是「明天 06:00」。**只有在內建排程器啟用時
  才會走到**（目前主權在 timer），所以擱著；已記在 `job_board.py` 的
  `_compute_next_run()` docstring 與 `../DECISIONS.md` 的已知限制。
- **systemd 本身的行為**：`Persistent=true` 補跑、`Type=oneshot` 的逾時、
  `copytruncate` 輪替，都是實測記錄在 `../DECISIONS.md`，但**沒有回歸測試**
  —— 升級 systemd 或改 unit 後必須重測。
- **爬蟲與 Flask 路由／前端**：完全沒有自動化測試。

## 回復到舊制（內建排程器）

程式碼從未被刪除，只是被閘門擋住：

```bash
systemctl --user disable --now jobscan.timer jobboard-logrotate.timer
systemctl --user edit jobboard.service    # 加：Environment=JOB_BOARD_INTERNAL_SCHEDULER=1
systemctl --user restart jobboard.service
```

**這份程序在 2026-09-20 之前是虛構的**，而且失敗得無聲無息：閘門打開後執行緒
確實啟動（log 有 `Scheduler thread started`），但

| 欄位 | 當時的結果 | 後果 |
|---|---|---|
| `schedule.enabled` | 停在 `false`（先前被啟動時壓平過） | 排程器**永遠不觸發** |
| `schedule.managed_by` | 仍是 `"systemd-timer"` | 前端 `SCHEDULE_READONLY=true` → 面板唯讀，**無法從 UI 重新啟用** |
| `schedule.next_run` | 沒算 | 面板顯示「下次執行：—」長達數小時 |

也就是說：照著上面做完，你會得到一個**再也不會掃描、而畫面顯示排程由一個剛剛被
停用的 timer 管理**的系統，只能手改 `.job_board_schedule.json` 才救得回來。
現在啟動時會自動把 `managed_by` 設回 `internal`、`enabled` 還原、`next_run` 算出來，
並在日誌印一行 `[scheduler] Internal scheduler ENABLED`。**J 區兩項檢查守著這件事，
M14／M15 兩個變異確認它們真的會響。**

> 為什麼選「自動啟用」而不是「把面板改成可編輯、讓使用者自己按」：設定這個環境
> 變數的語意就是「我要舊制」，而舊制＝排程會運作。留成一個需要人再按一次才能動
> 的狀態，等於把同一個坑換個位置。並發由 `run_scan.sh` 的 `flock` 吸收
> （兩條路徑同時觸發只會有一個真的跑）。

注意：內建排程器仍有那個**硬性 6 小時補跑視窗**，所以回復等於接受
「12:30 之後才喚醒 → 整天不掃描」這個原始問題。
