# DECISIONS.md

重要決策紀錄 — 依專案工作流程要求更新。

## 2026-08-19 — dashboard 改以 systemd user service 常駐

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
