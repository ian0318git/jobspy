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

**這個專案的自動化測試只有一支**：`tests/test_scan_lock.py`（81 項檢查）。
其餘全部是手動驗證 —— 上面各節的「驗證」指令就是手動程序。

```bash
cd /home/ian/github-project/jobspy
.venv/bin/python tests/test_scan_lock.py     # 通過時印「✅ 全數通過」且 exit 0

# 要引用「幾項」時用這個量，不要憑印象寫 —— 這個數字已經腐化過七次
# （28→39→53→55→56→58→65→81）。第 65→81 那次是第七輪退回：L 區 9 項 + M 區 7 項。
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
| H | 2 | 日誌每行必須**一次** `write()` 寫出（第五輪退回，見下） |
| I | 1 | `job_board.py` 不得有 live 的 `print()`（第五輪退回） |
| J | 2 | 啟動時的排程主權轉移：兩個方向都要正確（見「回復到舊制」一節） |
| K | 7 | `api_schedule` 的輸入驗證：幽靈排程（`times: []`）、非物件主體、`enabled` 的字串陷阱、第三道鎖（不得重新武裝）（第六輪退回） |
| L | 9 | **跨午夜的掃描被靜默跳過**（MAJOR-1）、`post-run` 的 `next_run` 必須等於 `_compute_next_run`、`interval_hours` 的 `inf`、非 ASCII 的「數字」（第七輪退回） |
| M | 7 | 損壞排程檔的**靜默降級**、`save_schedule` 的原子性、`ok` 的語意、`TimerCalendar` 解析（第七輪退回） |

> ⚠️ **H 那一格是錯的，而且錯了兩輪。** 第七輪審查 MAJOR-2：原本寫 `| H | 3 |`，
> 全表加總 66，與權威的 65 不符。實際拆開是 H=2、I=1、J=2 —— 因為 J 區的兩項
> 沒有印自己的 `=== J. ===` 標題，`awk` 那種以標題分組的數法會把它們算進 H。
> **用標題分組的計數器會把沒有標題的區段算給前一個區段**；要引用項數請用上面
> 那條「權威指令」，不要用分組數字相加。

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
- **第二道跳線：`subprocess.Popen`**（第六輪審查的 MAJOR M2）。排程器啟動爬蟲走的是
  `subprocess.Popen([sys.executable, "-u", "linkedin_job_search.py"])`，
  **完全繞過只擋 `subprocess.run` 的那道**。也就是說第六輪之前的隔離不是護欄，
  而是「種子排程的 `interval_hours` 剛好很大」這個常數 —— 種子一逾期（那一輪真的
  發生過兩次，`job_board.log` 兩筆 `[scheduler] Triggering search`），子行程會在
  **生產目錄**跑起真的爬蟲。審查員用原封不動的跳線實測重現。現在它是一條斷言。
  **兩道都必須裝在 `import job_board` 之前** —— `job_board` 在模組層就啟動背景
  執行緒，插在其後會有 race。
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

目前 **21 個變異、19 個被逮、2 個已知逃脫**（M13、M20；`mutate.py` 的
`EXPECTED_ESCAPES` 把「已理解的逃脫」與「沒被發現的覆蓋缺口」分開回報，只有後者
會讓退出碼變 1。M20 的用意正是**讓一個覆蓋缺口變成機器看得見的事實**，而不是
文件裡的一句話）。**若你新增修正卻找不到會失敗的變異，代表那個修正沒有被測試
覆蓋** —— 那就把那個變異加進來、列進 `EXPECTED_ESCAPES`，讓缺口誠實地站出來。

> ⚠️ **這個工具不會碰生產目錄。** 第五輪審查抓到：舊版直接改寫 repo 裡的
> `job_board.py`，當時的 13 個變異每個會在磁碟上存在 5–25 秒，而 `jobboard.service`
> 是 `Restart=always` + `MemoryMax=512M` —— 若這段窗口內被 OOM 殺掉而重啟，
> **新行程載入的就是那個變異**（清單裡有 M5／M6／M8，其中 M8 是停滯偵測整個
> no-op）。現在改用 `git archive HEAD` 解到 `/tmp` 的隔離副本，生產檔案從頭到尾
> 不被寫入，結束時以 sha256 驗證。

> **變異表的數字是可重現的。** 這句話在 2026-09-20 之前是**假的** —— J 區有一項
> 會間歇性失敗（見下方第五個坑），同一份程式碼連跑兩次會得到不同的 P/F。
> 修掉之後**連跑四次逐項相同**（第一次只跑兩次就下結論，被審查員用第三次推翻 ——
> 兩次不是證據）。**若你看到數字漂移，先當成有東西不穩定，不要當成「本來就會
> 這樣」。**
>
> ⚠️ **跑的時候不要動 `job_board.py`。** 工具會在結束時比對生產檔案的 sha256；
> 中途改檔（即使只是加一行註解）會讓它回報 sha 不符。那個回報是**對的** ——
> 但該輪的數字就不能再當成證據，得重跑。（第六輪真的發生過：我在背景跑的時候
> 順手改了 `job_board.py`。）
>
> ⚠️ **看到那個 sha 警告，先用眼睛看 `git diff`，不要反射性 `git checkout`。**
> 「檔案變了」有兩個成因、處置相反：**變異外洩**（只有突變那幾行 → `git checkout`
> 是對的）與**你自己在跑的期間編輯過**（→ `git checkout` 會把你剛寫好的工作
> 整批刪掉）。一個只知道「檔案變了」的偵測器，不該建議一個會刪掉未提交工作的
> 動作；`mutate.py` 的訊息已經改成先請你看 diff。

六個踩過的坑，寫在這裡免得重蹈：

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
  **修法是子行程結束前 `sys.stdout.flush()`，再 `os._exit(0)` 跳過 finalization。**
  > ⚠️ **這一段的數字我發佈過兩次，兩次都被推翻 —— 一次是審查員重測不出來，
  > 一次是我自己重測不出來。** 原因是那些數字來自沒有描述清楚的子行程組態，
  > 而**沒寫組態的數字不是證據**。現在量測本身進了版控：
  >
  > ```bash
  > .venv/bin/python tests/sigabrt_probe.py 200   # 三個組態，各 N 次
  > ```
  >
  > 四個獨立量測（每次都有 A/B/C 三組，N 分別是 60、60、20、200）：
  >
  > | 組態 | 第一次 60 | 第二次 60 | 20 | **200** | 合計 |
  > |---|---|---|---|---|---|
  > | A ＝ 現在的子行程**拿掉** `os._exit(0)`（保留 `flush()`）、管線 | 0 | 0 | 1 | **2** | **3/340 ≈ 0.9%** |
  > | B ＝ `d80fb93` 的原始版（沒有 flush、沒有 `os._exit(0)`）、管線 | 15 | 7 | 3 | **23** | **48/340 ≈ 14%** |
  > | C ＝ 同 B，但 stdout 導到**檔案** | 3 | 6 | 2 | **22** | **33/340 ≈ 10%** |
  >
  > **可以下的結論**：
  > - **`flush()` 不足以讓它消失。** A 不是 0 —— 我曾經發佈「A = 0/60，
  >   所以 flush 就夠了」，那句話在 N=20 那次就出現 1 次 ABRT。
  >   ≈1% 看起來很小，但一輪測試會 spawn 這個子行程好幾次 —— **隨機紅會留下來**，
  >   而隨機紅的測試最後會被人加 `|| true` 繞過。
  >   **真正讓它結構上不可能的是 `os._exit(0)`：它根本不進 finalization，
  >   也就不需要去搶那個鎖。兩個都留著是對的。**
  > - **B 的比率約一成，不是 25%。** 兩次 60 次的量測差了兩倍以上（15 vs 7），
  >   所以「25%」這種點估計不該被引用。
  > - **「管線比檔案更容易踩到」不成立。** 我原本根據 15/60 vs 3/60 下了這個結論，
  >   但 N=200 那兩組是 23 vs 22 —— 管線與檔案在這個尺度上分不出來。
  >   **兩組 60 次的樣本差異，小於量測本身的run-to-run 變異。**
  >
  > **這不是生產缺陷，但理由要說對。** 原本只寫了「SIGTERM 是 `SIG_DFL`、不跑
  > finalization」，那只涵蓋 `systemctl stop` 那條路。生產**還有**正常結束的路徑
  > （werkzeug `serve_forever` 攔到 `KeyboardInterrupt`、埠被佔用時的 `SystemExit`），
  > 審查員實測那兩條 6/6 rc=-15、20/20 rc=1，**ABRT 0 次**。真正的護欄是 unit 裡的
  > `Environment=PYTHONUNBUFFERED=1`：它讓 `sys.stdout.buffer` 變成 **`FileIO`**
  > 而不是 `BufferedWriter`（本機實測：未設 → `TextIOWrapper/BufferedWriter`，
  > 設了 → `TextIOWrapper/FileIO`）—— 而那個 fatal error 的訊息點名的是
  > `BufferedWriter`。**拿掉 `PYTHONUNBUFFERED=1`，這條推理就整段失效。**
  > **任何這裡新寫的測試只要用子行程 import `job_board`，就必須 `flush()` 後
  > `os._exit(0)` 收尾**，否則你會得到一個隨機紅的測試。
  > 測試子行程繼承的是測試環境，**不是** unit 的 `PYTHONUNBUFFERED=1`。

- **⚠️ 測試「崩潰」與測試「失敗」在變異表上看起來一樣 —— 都是 0 個 FAIL**：
  這是第六輪我自己踩到的。`check()` 只有在被呼叫時才會印 `[FAIL]`，而
  `mutate.py` 數的就是 `[FAIL]`。所以一個讓測試**在跑到斷言之前就 traceback
  死掉**的變異，會得到 0 個 FAIL、看起來「什麼都沒抓到」→ 被記成逃脫。
  實際發生的地方：K 區第一版用 `mock.patch.object(jb, "jsonify", side_effect=
  lambda **k: k)`，而產品碼寫的是 `return jsonify(resp)`（**位置**引數）→
  TypeError → 整個測試檔死在 K 區第二項，後面全部不執行。
  **修法有兩層**：(1) 測試碼自己把例外收成 dict（`_post_schedule` 的
  `except Exception` 回 `{"__error__": ...}`）；(2) 任何「讀檔／呼叫產品碼」
  的輔助函式一律不讓例外冒出去（`_startup_schedule` 的排程檔讀取同理）。
  **看到某個變異逃脫，先確認測試是「跑完之後有 FAIL」還是「根本沒跑完」。**

### ⚠️ 未涵蓋（不要以為有測試就安全）

- **`linkedin_job_search.py` 有同一個兩次 syscall 的結構**（56 個 `print`，
  `jobscan.service` 同樣 `PYTHONUNBUFFERED=1`）。今天沒有症狀（`_worker` 不 print，
  `cron_search.log` 用雙時戳啟發式檢查 0 筆），但若爬蟲改多執行緒輸出、或
  `run_scan.sh` 的 echo 與爬蟲的 print 交錯，同一個缺陷會在另一份日誌復發。
  **I 區的 AST 掃描只涵蓋 `job_board.py`。**
- **`run_scan.sh` 的 bash 端：零自動化測試。** 鎖重試、`SKIPPED` 路徑、`LOCK_WAIT`
  驗證、`finish()`／`trap` 的退出碼語意，全部只有手動驗證過。改這支腳本時請照
  上面「手動操作」一節實測。
- **前端 JS：零自動化測試。** `updateSchedule()` 現在會顯示後端回傳的
  `warning`／`error`（第六輪補的 —— 在那之前，請求被拒絕與存檔成功在畫面上長得
  一模一樣）。**沒有任何測試守著它**。刻意不加一個「grep 有沒有 `s.warning`」
  的檢查：那種測試只會給假信心（README 上面才剛說過「殘留檢查不能用 grep」）。
  改到那段 JS 時請用手動驗證：devtools 送 `{"times": []}`，確認跳 alert。
- **stray holder 造成的卡死**：`trigger=manual` 的手動掃描沒有 unit 可等，
  而 stray holder 持鎖時 state 停在上一輪的 `finished` → phase 閘門直接 return →
  **沒有任何機制會放掉那把鎖**。已知、未修，理由與解法見 `../DECISIONS.md`
  第四輪條目的「已知限制」。
- **排程器「跑完之後」算下一次的那段：已修，但結構上測不到（＝沒有回歸保護）**。
  第六輪修掉了：`scheduler_loop()` 跑完一輪後原本用 `times[0]`（**未排序**）算下
  一個時段，而 UI 儲存那條用 `sorted(times)` —— 兩條路徑對同一個 cfg 有不同解讀。
  現在兩邊都是 `sorted(cfg.get("times") or [...])`，連 `or`（而非 `get(k, default)`）
  也對齊了，所以舊版寫進檔案的空清單 `[]` 在兩邊都會退回預設時段。
  **但這段程式碼要 `JOB_BOARD_INTERNAL_SCHEDULER=1` 而且得先跑完一輪 19 分鐘的
  真掃描才會執行**，測試兩個前提都不成立。變異 `M20` 就是這一段，它**保證逃脫**，
  列在 `EXPECTED_ESCAPES` 裡是刻意的：讓這個缺口是機器看得見的事實，而不是
  文件裡的一句安慰。**改到這段時請手動驗證**（開閘門、把 `times` 存成
  `["22:00","06:00"]`、確認跑完後 `next_run` 是隔天的 06:00）。
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
