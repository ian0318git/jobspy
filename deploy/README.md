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

**這個專案的自動化測試只有一支**：`tests/test_scan_lock.py`（39 項檢查）。
其餘全部是手動驗證 —— 上面各節的「驗證」指令就是手動程序。

```bash
cd /home/ian/github-project/jobspy
.venv/bin/python tests/test_scan_lock.py     # 通過時印「✅ 全數通過」且 exit 0
```

| 區段 | 項數 | 涵蓋 |
|---|---|---|
| A / A2 | 2 + 3 | `SEARCH_LOCK` 必須可重入（CRITICAL-1）、完整 `kill_stalled_search()` 路徑 |
| B | 4 | 過期 owner 不得釋放現任鎖（含 8 執行緒 hammer） |
| C | 5 | 看板持鎖時不得留下凍結的 `active` 狀態 |
| D | 6 | Popen 失敗時必須把鎖還回去（MAJOR-3） |
| E | 5 | 孤兒鎖自癒 |
| F | 14 | 孤兒鎖盲區、誤殺無關行程、幻影掃描、假成功（第三輪退回） |

**測試本身有護欄**（因為它 import `job_board` 會連帶啟動背景 watchdog）：

- 鎖檔指向 `tempfile.mkdtemp()` 的私人檔案 —— **絕不碰生產的 `logs/jobscan.lock`**。
  這一項是第二輪審查的 MINOR-9：原本測試會持有全機鎖約 53 秒，若此時 timer 觸發，
  真實掃描就會白等 20 秒後 SKIP ——**測試本身能製造這次遷移要消滅的失敗**。
- `kill_stalled_external()` 以 no-op 取代（背景執行緒走模組全域）；
  要驗證真正的護欄時，測試改呼叫保存下來的原函式 `_REAL_KILL_EXTERNAL`。
- `run_scan.sh` 的 `LOCK`/`LIVE`/`STATE` 可用環境變數覆寫，就是為了讓手動的
  端到端測試**不污染生產的 `search_state.json`**（第三輪審查的 MINOR）。

**以變異實驗自我驗證**：每一項修正都做過「把它改回去，確認測試會 FAIL」。
沒有 FAIL 的修正等於沒有回歸保護。目前 5 個變異全數被捕捉（詳見 `../DECISIONS.md`
第三輪條目）。**若你新增修正卻找不到會失敗的變異，代表那個修正沒有被測試覆蓋。**

### ⚠️ 未涵蓋（不要以為有測試就安全）

- **`run_scan.sh` 的 bash 端：零自動化測試。** 鎖重試、`SKIPPED` 路徑、`LOCK_WAIT`
  驗證、`finish()`／`trap` 的退出碼語意，全部只有手動驗證過。改這支腳本時請照
  上面「手動操作」一節實測。
- **`kill_stalled_external()` 的實際動手路徑**：會殺真行程，測試刻意不碰
  （只驗證「拒絕動手」與「不該呼叫 systemctl」）。真的動手只在本案的事故重現中驗證過。
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

注意：內建排程器仍有那個**硬性 6 小時補跑視窗**，所以回復等於接受
「12:30 之後才喚醒 → 整天不掃描」這個原始問題。
