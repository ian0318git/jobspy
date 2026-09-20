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

## 實機驗證記錄：休眠喚醒後的自動補跑（2026-09-20）

這一節記的是**原始需求「電腦晚上會休眠，醒來要自己補跑」在生產環境的實際達成**，
不是 lab 條件。全部是既成事實的觀測，沒有為了驗證去動任何設定。

**時序**（來源：journal、`search_state.json`、`cron_search.log`、stamp mtime）

| 時刻 | 事件 | 證據 |
|---|---|---|
| 04:30（機器睡著） | `jobboard-logrotate.timer` 的時段被錯過 | — |
| 06:00（機器睡著） | `jobscan.timer` 的時段被錯過 | — |
| 08:48:59 | **VM 跟著宿主機喚醒**，realtime 跳動 | `systemd-resolved: Clock change detected. Flushing caches.`（連續兩筆） |
| 08:48:59 | `jobscan.timer` **補跑 06:00 那一輪** | `stamp-jobscan.timer` mtime = `08:48:59.518` |
| 08:49:01 | `jobboard-logrotate.timer` **補跑 04:30 那一輪** | `stamp-jobboard-logrotate.timer` mtime = `08:49:01.851` |
| 08:49:02 | 日誌輪替（copy + truncate）完成 | `logrotate[283396]: truncating .../job_board.log` |
| 08:49:03 | `run_scan.sh` 開始，state 寫入 `phase=running` | `search_state.json` |
| 08:49:07 | 看板偵測到外部掃描 | `job_board.log`（**輪替後的新檔**） |
| 09:07:55 | 掃描結束，`exit_code=0`（歷時 18 分 52 秒） | `search_state.json` |
| 09:07:58 | 看板**自動切換**到新結果檔（40 筆） | `job_board.log`：`已自動切換到 kanban_jobs_20260920_0849.json（40 筆）` |
| 09:07:55 後 | NEXT 恢復為 `Sun 2026-09-20 22:00:00 AEST` | `list-timers` |

**這一輪同時把三個原本「只有 lab 證據」的路徑變成生產證據：**

1. **`Persistent=true` 的休眠補跑**（Stage 5）。原本的 lab 做法是刪 stamp + drop-in 造假
   一個已過的時刻；這次是 VM 真的睡過 06:00、真的在 08:48:59 醒來、真的補跑。
   這也是整個遷移要解決的那一件事。
2. **`logrotate` 由 timer 觸發**（Stage 7 唯一未證的那條路）。原本 `list-timers` 的
   LAST 是 `-`（只有手動 `logrotate -f` 的證據）；現在是真的 timer→service。
   而且它與掃描在**同一秒**發生，正好把「輪替會不會打斷掃描」也一起答了：不會。
3. **`copytruncate` 之後 dashboard 仍寫進新檔**（Stage 7 的「最關鍵」判準）。
   `job_board.log` 在 08:49:02 被截斷，08:49:07 的寫入落在**新檔**；
   輪替前後的 inode 分別是 408086 / 397974（`copytruncate` 不是 rename），無 NUL 空洞。

**仍未證的一條**：機器**醒著**時的正常排程觸發（例如 22:00 到點那一刻，機器沒睡）。
上面補跑的那一輪走的是 `Persistent` 的補跑路徑，不是「時間到就觸發」那條。
兩者在 systemd 裡是同一個 `timer_enter_waiting()` 算出來的 elapse，差異只在
「錯過的要不要補」，但**這是推論，不是觀測**。下一次機器醒著的 22:00 會自動補上這個證據。

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

**`copytruncate` 的一個已知副作用（2026-09-20 實際踩到，不是缺陷，但要知道）**：
`cp` 與 `truncate` 之間寫進去的那一行會被截掉 —— 但它已經被 `cp` 抄進輪替檔了，
所以**那一行不會消失，只是跑到輪替檔裡去**。實例：08:49:02 輪替、08:49:03 那一輪
開跑，`run_scan.sh` 的 `START` 行正好落在窗口內，於是它出現在
`logs/cron_search.log-2026-09-20` 的**最後一行**，而新的 `logs/cron_search.log`
從標題橫幅開始。追事故時若比對兩個檔卻只看到半條線索，原因在這裡。
（逐字稿本身不受影響：`run_scan.sh` 另外 `tee` 到 `logs/search_current.log`。）

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

---

## ⚠️ `list-timers` 的 NEXT 顯示 `-` 是正常的（不要當成排程壞掉）

**症狀**：`systemctl --user list-timers jobscan.timer` 的 `NEXT` 欄是 `-`。
一天會看到兩次，每次約 19 分鐘 —— 也就是**每一輪掃描正在跑的時候**。

**這是 systemd 的正常表示法**：timer 的那次 elapse 已經被消費掉了，下一次還沒算出來
（也不需要喚醒）。**服務一結束就會自己恢復。**

> ⚠️ **觸發條件不是「service 正在跑」，而是「timer 已經把那次 elapse 交出去、
> 下一次還沒算」。** 這兩者不一樣，而差別在實測時咬過我一次：手動
> `systemctl --user start <unit>.service` 拉起的服務，`NEXT` 是**正常有值**的，
> 因為 timer 的那次 elapse 還在。生產的 `jobscan.timer` 是**自己**觸發 service 的，
> 所以才會看到 `-`。**用「service 在跑」去解釋它，會解釋錯。**

2026-09-20 的量測（`NEXT` ／ `NextElapseUSecMonotonic` ／ `list-timers --output=json` 的 `next`）：

| 狀態 | `NEXT` | `NextElapseUSecRealtime` | `NextElapseUSecMonotonic` | JSON `next` |
|---|---|---|---|---|
| timer **未**觸發、service 未跑 | `Sun 09:14:00 AEST` | `Sun 2026-09-20 11:22:00 AEST` | **`0`** | `1789859640000000`（int） |
| timer **未**觸發、service **手動**拉起 | `Sun 09:14:00 AEST` | 同上 | **`0`** | 同上 |
| **timer 已自行觸發**、其 service 仍在跑 | **`-`** | **（空）** | **`infinity`** | **`None`（null）** |
| service 結束後 | 下一個 `:00` | 下一次的值 | `0` | 下一次的值 |
| **生產的 `jobscan.timer`**（08:49:02→09:07:55） | `-` | （空） | `infinity` | — |

> ⚠️ **2026-09-20 更正（第八輪 NIT-3）：上一版這張表的第三欄寫「有值」，那是錯的。**
> 當初是用 `systemctl show` 讀**位置欄位**，而它印的是**固定的規範順序**
> （`Realtime` 在前、`Monotonic` 在後），所以把 Realtime 的值看成了 Monotonic 的。
> 實測第一列是 `Realtime=Sun …11:22:00 AEST` / **`Monotonic=0`**。
> 現在改用 `systemctl show … -p NextElapseUSecRealtime -p NextElapseUSecMonotonic`
> **逐項指名**，讓這種誤讀在結構上不可能。
>
> **而更正之後看到的比原本記的更有用：這兩欄不是同一個值的兩種視圖，值會搬家。**
> 尚未觸發時下次觸發是一個**牆鐘時刻**（Realtime 有值、Monotonic `0`）；
> 已觸發而下次還沒算時，它**不再等**（Realtime 空、Monotonic `infinity`）。
> 哪一欄有值，取決於 systemd 當下用哪個時鐘在想「下一次」。
> 所以 `infinity` 不是「一個很大的牆鐘時間」，`Monotonic=0` 也不是「1970-01-01」。
>
> 🔴 **這正是寫錯那一格的實質後果**：`get_timer_state()` 若改用 `systemctl show`
> 取 `NextElapseUSecMonotonic`（名字看起來最像「倒數」），正常狀態下拿到的是
> **`0`** → 會被當成 `1970-01-01` 的「下次觸發」。**文件裡犯的誤讀，就是程式碼
> 換一個屬性會犯的同一個錯。**

probe 的可重跑指令（自己建、自己收，不碰 jobspy 的 unit）：

```bash
systemd-run --user --unit=jwprobe2 --on-calendar='*-*-* *:*:00' /bin/sleep 30
# 手動 start 它的 service → NEXT 仍然有值（timer 的 elapse 還在）
systemctl --user start jwprobe2.service
# 等 :00 到、timer 自己觸發 → 此時 NEXT 才是 "-"、JSON 的 next 才是 null
systemctl --user list-timers jwprobe2.timer --all --output=json --no-pager
systemctl --user stop jwprobe2.service jwprobe2.timer
systemctl --user reset-failed jwprobe2.service jwprobe2.timer
```

**那個 `None` 很重要，不要漏掉**：`get_timer_state()` 走的就是
`list-timers --output=json` 的 `u.get("next") or None`。量到的是 **`null`**，
不是 `0`（會變成 `1970-01-01`）也不是 `INT64_MAX`（會變成幾億年後）——
所以 `next_iso` 就是 `None`，啟動 banner 的 `_timer_next_text()` **真的會走到那個分支，
不是死碼**。若哪天 systemd 改回傳 `0` 或 `INT64_MAX`，那個修正會**靜默失效**
（畫面會顯示一個假的下次觸發時間），重測時請先看這一格。

**同一個時間點還有一個陷阱**：`TimersCalendar` 的 `next_elapse=` 會**停在已經過去的
值**，所以它**不能**拿來當 `next` 的退路。實測（同一輪掃描）：

```
執行中：{ OnCalendar=*-*-* 06:00:00 Australia/Melbourne ; next_elapse=Sun 2026-09-20 06:00:00 AEST }  ← 08:49 時已是過去
結束後：{ OnCalendar=*-*-* 06:00:00 Australia/Melbourne ; next_elapse=Mon 2026-09-21 06:00:00 AEST }  ← 正確
```

拿它來填 `next` 會把一個**過去的時間**標成「下次觸發」——那比空著更糟，因為它看起來
像一個答案。所以 `get_timer_state()` 在這種情況下就是回 `next_iso: null`，
前端據此**省略**「下次 …」那一段（不是顯示錯誤的值），啟動 banner 則明說
「掃描執行中，systemd 尚未計算下一次」。

> 這一段值得記下來的原因是它跟本專案反覆在抓的**幽靈排程方向相反**：不是承諾一件
> 不會發生的事，而是**否認一件會發生的事**。原本 banner 一律印「下次觸發: (無)」，
> 若啟動時剛好撞上掃描窗口，看板上就會留下一句讀起來像「排程壞了」的話。

---

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

**這個專案的自動化測試只有一支**：`tests/test_scan_lock.py`（90 項檢查）。
其餘全部是手動驗證 —— 上面各節的「驗證」指令就是手動程序。

```bash
cd /home/ian/github-project/jobspy
.venv/bin/python tests/test_scan_lock.py     # 通過時印「✅ 全數通過」且 exit 0

# 要引用「幾項」時用這個量，不要憑印象寫。
#
# ⚠️ 第九輪【審查 NIT-1】更正：這裡原本寫「這個數字已經腐化過十次」，但括號裡
# 是 **12 個值＝11 個轉折**，而下一個段落又自稱「第十次」—— 兩個數字互相矛盾，
# 而且**兩個都沒有可重跑的出處**。那正是這一段自己在警告的那件事，發生在這一段
# 自己身上。現在只留【可以從這條鏈驗證】的事實：它是實際的遞增史，共 11 個轉折。
# （DECISIONS.md 有一次把它記成「第四次」，那是當時的計數，不是現在的。）
#   28→39→53→55→56→58→65→81→83→87→89→90
#   第九輪【審查退回】的修正**沒有動到項數** —— MINOR-1 是把一項競態斷言換成
#   有界等待（同一個 check()），MINOR-3 改的是變異載具。所以鏈上沒有新的一格。
#   65→81 是第七輪退回：L 區 9 項 + M 區 7 項。
#   81→83 是第八輪自查：F 區 +1（F4 補回漏掉的 subprocess.run 攔截）、
#           M 區 +1（banner 的「下次觸發」不得把「執行中」印成「(無)」）。
#   83→87 是第八輪【審查退回】：C 區 +1 與 G 區 +1（未攔 subprocess.run 的
#           三個呼叫點補上攔截與斷言）、L 區 +2（_fired_list 的 6 種畸形值
#           與正向控制）。
#   87→89 是第九輪：J 區 +2（子行程必須真的採用 JOBSCAN_* env —— 守著 MINOR-3）。
#   89→90 是第九輪：L 區 +1（AST 靜態不變式：_fired_today 的原始讀取只准出現在
#           _fired_map() —— 守著 MINOR-2 的【另一半】）。
#           ⚠️ 這兩組都是**修完之後回頭補的**。兩個缺陷（MINOR-2／MINOR-3）都被
#           修好了，但當時都沒有任何檢查守著它們的關鍵那一半：
#             * 拿掉 JOBSCAN_* env 讀取 → 全綠（實測）
#             * 把 scheduler_loop 的讀取退回原生 .get() → 全綠（實測）
#           對應變異 **M31** 與 **M32**，兩者都已確認會被逮。
#           ⚠️ 下面這兩個讀數是【量測當時的總項數】→ 通過/失敗，不是現在的：
#             補 J 區那 2 項時總數是 89 → M31 讀出 87P/2F
#             補 L 區那 1 項時總數是 90 → M32 讀出 89P/1F
#           在【現在】的 90 項下重跑 → M31 讀出 88P/2F、M32 讀出 89P/1F（已實測，見變異表）。
#           ⚠️ 同一個變異在不同版本的測試套件下讀數不同，而兩個讀數都是對的 ——
#              引用讀數時一定要連「當時總共幾項」一起講。
#
# ⚠️ 這條註解本身就是「說會腐化、然後就腐化了」的又一個實例（第九輪 NIT-1：
#    它宣稱的次數與自己列的清單對不上）。**不要相信這裡的數字，跑上面那條 grep -c。**
#    它腐化的方式是「每一次補完檢查忘了回來改這一行」——
#    而這正是本專案反覆抓到的那個形狀。
.venv/bin/python tests/test_scan_lock.py | grep -c '\[PASS\]'
```

> ⚠️ **跑測試請用 `.venv/bin/python`。** 用系統 `python3` 會停在
> `ModuleNotFoundError: No module named 'flask'`。`tests/mutate.py` 現在會自己
> 選對解譯器，並且**先跑一次未變異的基準線**（要求 `✅ 全數通過` 且 0 失敗），
> 通過了才開始跑變異 —— 否則整張表都只是同一個既有故障的回音，而它看起來一模一樣。

| 區段 | 項數 | 涵蓋 |
|---|---|---|
| A / A2 | 2 + 3 | `SEARCH_LOCK` 必須可重入（CRITICAL-1）、完整 `kill_stalled_search()` 路徑 |
| B | 4 | 過期 owner 不得釋放現任鎖（含 8 執行緒 hammer） |
| C | 6 | 看板持鎖時不得留下凍結的 `active` 狀態；C 區兩個 `_REAL_KILL_EXTERNAL()` 呼叫點不得碰 `systemctl`（第八輪審查） |
| D | 6 | Popen 失敗時必須把鎖還回去（MAJOR-3） |
| E | 5 | 孤兒鎖自癒 |
| F | 18 | 孤兒鎖盲區、誤殺無關行程、幻影掃描、假成功、`cmdline` 身分驗證（第三輪退回）；F4 的 `subprocess.run` 攔截（第八輪自查） |
| F7 | 7 | 殺戮路徑的**正向**覆蓋：必須開火、且目標真的死掉（第四輪退回） |
| G | 5 | `errors="replace"` 與 TOCTOU 重檢的回歸保護（第四輪退回）；TOCTOU 路徑不得呼叫 `systemctl`（第八輪審查） |
| H | 2 | 日誌每行必須**一次** `write()` 寫出（第五輪退回，見下） |
| I | 1 | `job_board.py` 不得有 live 的 `print()`（第五輪退回） |
| J | 4 | 啟動時的排程主權轉移：兩個方向都要正確（見「回復到舊制」一節）；`JOBSCAN_*` env 隔離必須是真的（第八輪 MINOR-3，對應變異 M31） |
| K | 7 | `api_schedule` 的輸入驗證：幽靈排程（`times: []`）、非物件主體、`enabled` 的字串陷阱、第三道鎖（不得重新武裝）（第六輪退回） |
| L | 12 | **跨午夜的掃描被靜默跳過**（MAJOR-1）、`post-run` 的 `next_run` 必須等於 `_compute_next_run`、`interval_hours` 的 `inf`、非 ASCII 的「數字」（第七輪退回）；`_fired_list` 對 6 種畸形 `_fired_today` 都不得丟例外 + 正向控制（第八輪審查）；`_fired_today` 的**原始讀取**只准出現在 `_fired_map()`（AST 靜態不變式，第九輪 MINOR-2，對應變異 M32） |
| M | 8 | 損壞排程檔的**靜默降級**、`save_schedule` 的原子性、`ok` 的語意、`TimerCalendar` 解析（第七輪退回）；banner 的「下次觸發」必須區分「沒裝」與「執行中還沒算」（第八輪） |

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

  > ⚠️ **2026-09-20 第八輪：這句話當時是錯的，而且差一點釀成事故。** F4 是
  > `_REAL_KILL_EXTERNAL()` 的呼叫點中**唯一沒有** mock `subprocess.run` 的
  > （F4b／F4c 都攔了），而它上方的註解正好寫著「任何直接呼叫的地方都必須攔下
  > 所有 systemctl 呼叫」。實際攔下它的是**這把跳線** —— 也就是說，那道「只在
  > 寫錯時響」的最後防線，真的響了。當時生產的 `jobscan.service` 正好在
  > `activating`（06:00 補跑那一輪），再往下就是對它送出真的 `SIGKILL`。
  >
  > 更陰險的是後果的第二層：跳線拋的例外會讓**整個測試檔當場崩潰**，而崩潰的
  > 行程 `[FAIL]` 數是 **0** —— `tests/mutate.py` 只看 `failed > 0`，於是把變異
  > M4 記成「逃脫」。同一輪稍早的表格裡 M4 還是「被逮」。**同一個變異的判定
  > 取決於生產 unit 當下的狀態**，而且**最嚴重的變異看起來最無害**。
  >
  > 兩邊都補了：F4 補上攔截與斷言；`tests/mutate.py` 在判定前先確認測試檔
  > **印出了收尾標記**（跑到底），否則記為 `INCONCLUSIVE` 而不是「逃脫」。
  > **教訓：寫在註解裡的規則不會自己執行。**
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

目前 **32 個變異、31 個被逮、1 個已知逃脫**（第九輪最終表的完整讀數見
`DECISIONS.md` 的「最終變異表」；`EXPECTED_ESCAPES = {"M13"}`；
`mutate.py` 把「已理解的逃脫」與「沒被發現的覆蓋缺口」分開回報，只有後者
會讓退出碼變 1）。**若你新增修正卻找不到會失敗的變異，代表那個修正沒有被測試
覆蓋** —— 那就把那個變異加進來、列進 `EXPECTED_ESCAPES`，讓缺口誠實地站出來。

> ⚠️ **`INCONCLUSIVE` 也【不】算逃脫**（第八輪新增）。測試檔必須印出收尾標記
> （`✅ 全數通過` 或 `項失敗：`）才算跑到底；沒印 → 記 `INCONCLUSIVE` 並讓
> 退出碼非 0。這個判定在第九輪**自己救了一輪**：整張表 30 個 `INCONCLUSIVE`、
> 真正的原因只是啟動載具時用了系統 `python3`（見上方「跑測試請用
> `.venv/bin/python`」）。若沒有這個判定，同一輪會被讀成「30 個逃脫」——
> 一個完全不存在的災難。**「量到 0」和「量尺壞了」在報表上長得一樣。**

> ⚠️ **但「列進 `EXPECTED_ESCAPES`」比它看起來危險得多。** 這裡曾經躺著第二個
> 豁免 `M20`，理由寫著「要 `INTERNAL_SCHEDULER=1` 且跑完一輪 19 分鐘的真掃描才會
> 走到，**結構上測不到**」。第七輪審查員證明那句話不成立 —— 帳務邏輯可以抽成純
> 函式，而**那段「測不到」的程式碼裡就藏著 MAJOR-1**（跨午夜掃描會靜默吃掉隔天
> 早上那一輪）。豁免清單把一個 MAJOR 藏在「已知且已理解」的標籤底下。
> **「測不到」通常只是「還沒抽出來」的另一種說法。**
>
> 剩下的 M13 是真逃脫，性質不同：它只拿掉 `_out()` 的 `threading.Lock`，
> 留下「整行一次 write」，所以剩下的是一個**競態**（兩條執行緒在同一行中間插隊），
> 在單一行程的測試裡是機率性的。它的觀測方式是人工的（連續跑 N 次、檢查有沒有
> `[scheduler][jobscan]` 這種黏行），**不是自動測試** —— 這一條是真的測不到，
> 而且已經理解。差別在於：M20 是「還沒試著測」，M13 是「試過了，測不到」。
>
> 第八輪還抓到自己一個更隱蔽的版本：**測試檔崩潰時 `[FAIL]` 數是 0**，
> 於是 `failed > 0` 的判定會把「最嚴重的變異」記成「逃脫」。現在要求測試檔
> 必須印出收尾標記才算數，否則記 `INCONCLUSIVE` 並讓退出碼非 0。詳見下方
> 「測試『崩潰』與測試『失敗』在變異表上長得一樣」一節。

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
  > | 組態 | 第一次 60 | 第二次 60 | 20 | **200** | 合計 | Wilson 95% CI |
  > |---|---|---|---|---|---|---|
  > | A ＝ 現在的子行程**拿掉** `os._exit(0)`（保留 `flush()`）、管線 | 0 | 0 | 1 | **2** | **3/340 = 0.9%** | **[0.3%, 2.6%]** |
  > | B ＝ `d80fb93` 的原始版（沒有 flush、沒有 `os._exit(0)`）、管線 | 15 | 7 | 3 | **23** | **48/340 = 14.1%** | **[10.8%, 18.2%]** |
  > | C ＝ 同 B，但 stdout 導到**檔案**（我測） | 3 | 6 | 2 | **22** | **33/340 = 9.7%** | **[7.0%, 13.3%]** |
  > | C ＝ 同上（**第七輪審查員另測**） | — | — | — | 36/200 | **36/200 = 18.0%** | **[13.3%, 23.9%]** |
  >
  > **可以下的結論**（第七輪 MAJOR-3 修正後的版本）：
  > - **`flush()` 不足以讓它消失。** A 不是 0 —— 我曾經發佈「A = 0/60，
  >   所以 flush 就夠了」，那句話在 N=20 那次就出現 1 次 ABRT。
  >   ≈1% 看起來很小，但一輪測試會 spawn 這個子行程好幾次 —— **隨機紅會留下來**，
  >   而隨機紅的測試最後會被人加 `|| true` 繞過。
  >   **真正讓它結構上不可能的是 `os._exit(0)`：它根本不進 finalization，
  >   也就不需要去搶那個鎖。兩個都留著是對的。**
  > - **C 不該有一個數字，只有一個範圍：≈ 7%~24%。** 我測 33/340（9.7%），
  >   審查員測 36/200（18.0%）—— 這兩個**互相矛盾**（z=2.79，p=0.0053），
  >   也就是說 C 的比率**不是那個組態的性質，而是當下環境的性質**
  >   （負載、排程時機、直譯器 build……我沒有定位出是哪一項，只能誠實說它會變）。
  >   **這一格是我第七輪被退回的三個 MAJOR 之一。**
  > - **B 的比率約一成，不是 25%。** 兩次 60 次的量測差了兩倍以上（15 vs 7），
  >   所以「25%」這種點估計不該被引用。
  > - **「管線比檔案更容易踩到」不成立**，而且**反過來也不成立**：
  >   B vs C 我測的那組是 z=−1.78、p=0.076 —— **等於沒有證據**。
  >   我原本把 B 與 C 列成兩列不同數字，那個排序是讀者自己會補上的結論，
  >   而它從來沒被支持過。
  > - **只有「A 遠小於 B 與 C」站得住**：三個對照全部 p<0.0001。
  >   差別在機制，不在樣本 —— A 不進 finalization，所以它不需要搶那把鎖。
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

  > **⚠️ 第八輪：上面那句提醒是給「人」看的，而人不會每次都在看。**
  > 同一個坑再踩一次，這次的變異是 **M4**（拿掉 phase 閘門），而它**是清單裡
  > 最嚴重的一個** —— 它會讓 F4 走到真的 `systemctl --user kill --signal=SIGKILL
  > jobscan.service`。測試檔被最底層的跳線擋下、當場崩潰、`[FAIL]` 數 **0** →
  > 記成「逃脫」。**最嚴重的變異看起來最無害。**
  >
  > 而且它還會**隨生產 unit 當下的狀態翻來翻去**：09:00 時 `jobscan.service`
  > 正好在 `activating`（06:00 的補跑），M4 就是在那個窗口從「被逮」翻成「逃脫」的。
  >
  > 所以判定不能靠提醒，要靠機械：**`mutate.py` 現在要求測試檔印出收尾標記**
  > （`✅ 全數通過` 或 `項失敗：`）。沒印＝沒跑到底 → 記 **`INCONCLUSIVE`**，
  > 明確標示「這不是逃脫」，並讓退出碼非 0。同時 F4 補上了它漏掉的
  > `subprocess.run` 攔截。
  >
  > ⚠️⚠️ **上面那句「它是七個 `_REAL_KILL_EXTERNAL()` 呼叫點裡唯一沒攔的」也是假的**
  > （第八輪審查用 AST 逐點重算，沒有採信我列的清單）：
  >
  > | 時點 | 呼叫點 | 其中沒被 mock 包住的 |
  > |---|---|---|
  > | 寫下那句話當時 | **9** 個（我寫「七個」） | **4** 個（我寫「唯一一個」） |
  > | 補上 F4 之後 | 9 個 | 3 個（C 區兩處、G 區 TOCTOU） |
  > | 本輪修完 | 9 個 | **0** 個 |
  >
  > 而且我還點名「TOCTOU 攔了」——**它正是那 4 個之一**。
  > **同一個缺陷（宣告 > 實際）在三段先後「宣告它已經修好」的文字裡各犯了一次。**
  >
  > **教訓：寫在註解裡的規則不會自己執行。** 那句鐵律註解寫得完全正確，
  > 而且就寫在違反它的那段程式碼上方 —— 差別在於它一直是「文字」而不是「判定」。
  >
  > **第二層教訓：「我列舉過了」不是證據。** 列舉的結果要能被別人重跑，
  > 而且重跑的方法本身不能把**註解**算成**實例** —— `grep -n "_REAL_KILL_EXTERNAL()"`
  > 在本檔 15 個命中裡有 6 個是註解，而我的第一版檢查腳本甚至把鐵律註解裡那句
  > `mock.patch.object(jb.subprocess, "run", ...)` 當成了真的 mock，於是判定變成
  > 「全部都有攔」。唯一可靠的是 AST 逐點判 enclosing（腳本見 DECISIONS.md）。

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
- **排程器「跑完之後」記帳的那段：已修，第七輪起有了回歸保護**。
  第六輪修掉了：`scheduler_loop()` 跑完一輪後原本用 `times[0]`（**未排序**）算下
  一個時段，而 UI 儲存那條用 `sorted(times)` —— 兩條路徑對同一個 cfg 有不同解讀。
  現在兩邊都是 `sorted(cfg.get("times") or [...])`，連 `or`（而非 `get(k, default)`）
  也對齊了，所以舊版寫進檔案的空清單 `[]` 在兩邊都會退回預設時段。

  > **⚠️ 這裡原本寫著「變異 M20 就是這一段，它保證逃脫，列在 `EXPECTED_ESCAPES`
  > 是刻意的」。第七輪審查員推翻了那個理由，而且那段程式碼裡就藏著 MAJOR-1
  > —— 跨午夜的掃描會把隔天早上 06:00 記成已觸發，靜默跳過。**
  > 修法是**不要推導**：due 判定 `break` 的當下就知道是哪一天的哪個時段，把它
  > 傳進模組層級純函式 `_record_run()` 即可。抽出之後 L 區測得到它，M20 也重新
  > 設計成「日期那一半」的變異，**現在是被逮的**（`81P/2F`）。
  > **「測不到」通常只是「還沒抽出來」的另一種說法。**
  >
  > 那段程式碼仍然只有在 `JOB_BOARD_INTERNAL_SCHEDULER=1` 時才會執行，但**帳務
  > 邏輯本身已經是純函式，測得到**。改到排程器時仍建議手動走一次：開閘門、
  > 把 `times` 存成 `["22:00","06:00"]`、確認跑完後 `next_run` 是隔天的 06:00。
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
