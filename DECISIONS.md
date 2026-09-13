# DECISIONS.md

重要決策紀錄 — 依專案工作流程要求更新。

## 2026-09-13 — 完整遷移到 Oracle VPS，以 SSH tunnel 存取 dashboard

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
