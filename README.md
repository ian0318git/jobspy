# 🔍 Melbourne Embedded Job Search

Tailored job scraper + Kanban board for embedded systems roles in Melbourne.

> **Forked from [JobSpy](https://github.com/cullenwatson/JobSpy)** by Cullen Watson —
> a multi-board job scraping library. This project adds semantic depth scoring,
> career track classification, security clearance filtering, and a Kanban web UI
> on top of the original `scrape_jobs()` engine.

## What this does

Two scripts:

| Script | Purpose |
|--------|---------|
| `linkedin_job_search.py` | Scrapes 4 job boards (LinkedIn, Indeed, **Seek**, **Jora**) for embedded jobs, scores them with 3 AI engines, outputs CSV + Kanban JSON |
| `job_board.py` | Flask web UI — Kanban board with status tracking, filtering, historical file browser, and auto-scheduler |

---

## Installation

```bash
git clone <this-repo>
cd jobspy

python3 -m venv .venv
source .venv/bin/activate
pip install pandas python-jobspy flask
```

---

## Usage

### 1. Run a search

```bash
./run_search.sh
```

> ⚠️ **不要直接 `python linkedin_job_search.py`。**
> `run_search.sh` → `run_scan.sh` 是全機唯一的爬蟲入口，它用 `logs/jobscan.lock` 的
> flock 保證手動掃描與 systemd timer 的排程掃描**不會並發**
> （2026-08-13 發生過兩套系統並發重複搜尋，各產一份結果檔）。
> 直接呼叫 python 會繞過這道鎖，也會讓看板的掃描狀態顯示失準。
> `Ctrl-C` / `SIGTERM` 會經由 `exec` 原樣傳到爬蟲。

即時輸出：`tail -f logs/search_current.log`　完整逐字稿：`tail -f logs/cron_search.log`

Output goes to `search_results/`:
```
search_results/
├── melbourne_embedded_jobs_20260714_0002.csv   ← Full results
├── kanban_jobs_20260714_0002.json             ← Top 32 for Kanban board
└── blocked_clearance_jobs_20260714_0002.json  ← Jobs blocked by clearance
```

Each run creates timestamped files — historical results are never overwritten.

### 2. Launch the Kanban board

看板以 **systemd user service** 常駐（`Restart=always`，開機自動啟動、不需登入）：

```bash
./run.sh     # = systemctl --user restart jobboard.service
./stop.sh    # = systemctl --user stop jobboard.service（真的停得住，見下）
```

Open `http://127.0.0.1:5000` in Chrome.

**只綁 loopback**（2026-09-19 起）。要從別台機器看請開 SSH 通道：
`ssh -L 5000:127.0.0.1:5000 <host>`，然後開 `http://127.0.0.1:5000`。

Idle resource usage: ~0.1% CPU, ~36 MB RAM（`MemoryMax=512M` 是失控成長的天花板）。

```bash
systemctl --user status  jobboard.service
journalctl --user -u jobboard.service -n 50
tail -f job_board.log
```

> 舊版 `stop.sh` 用 `pkill -f job_board.py`，會被 `Restart=always` 在 5 秒後復活
> —— 它從來沒有真正停掉過服務。新版走 `systemctl stop`（明確的 stop 不受 `Restart=` 影響）。

**排程由 systemd timer 負責，不是看板本身**（2026-09-19 起）：

| Timer | 時刻（Melbourne） | 用途 |
|---|---|---|
| `jobscan.timer` | 06:00 / 22:00 | 執行爬蟲。`Persistent=true` → **休眠喚醒後會補跑** |
| `jobboard-logrotate.timer` | 04:30 | 輪替 `job_board.log` 與 `logs/cron_search.log` |

```bash
systemctl --user list-timers --all | grep -E 'jobscan|logrotate'
```

改用的理由：內建排程器的補跑視窗是硬性 6 小時，**12:30 之後才喚醒就整天不掃描**
（這台 VM 跟著宿主機 suspend，無法自行醒來）。詳見 `DECISIONS.md` 2026-09-19 條目。

看板標題列有掃描狀態晶片，且能**看見不是它自己啟動的掃描**（timer 觸發的也算，
會即時跟讀輸出並在結束後自動換檔）。排程面板為**唯讀**：強制 POST `{"enabled":true}`
會被拒絕並回 `warning`（三道鎖防止雙軌觸發）。

> `python job_board.py` 前景執行仍可用於除錯，但它不會被 systemd 接管日誌，
> 且會與常駐服務搶 5000 埠。正式使用請走 `./run.sh`。

---

## How the 3 engines work

### 🧠 Semantic Depth Brain

4-layer signal system distinguishes genuine embedded roles from IT noise:

| Layer | Weight | Examples |
|-------|--------|----------|
| SILICON | ×15 | bare-metal, JTAG, DDR timing, secure boot, TrustZone, SerDes |
| KERNEL | ×10 | Linux kernel, device drivers, RTOS, BSP, U-Boot, Yocto |
| HARDWARE | ×6 | FPGA, DSP, I2C/SPI/UART, hardware bring-up, PHY |
| EMBEDDED | ×3 | firmware, ARM Cortex, cross-compilation, real-time |

IT noise (React, AWS, Kubernetes, AI/ML pipeline, frontend...) = **−8 each**.
Purity ratio filters out false matches.

### 🛤️ IC / Lead Dual Track

Classifies every job as:
- **💻 IC Track** — Senior/Staff/Principal/Fellow Engineer
- **👔 Lead Track** — Manager, Director, Head of Engineering
- **🎯 Hybrid** — Tech Lead, hands-on + mentoring

### 🚫 Security Clearance Red Line

Auto-detects and blocks jobs requiring NV1/NV2/Baseline clearance or Australian citizenship/PR — verify your eligibility before applying.

Blocked jobs go to a separate JSON file for reference (some may become relevant if visa status changes).

---

## Configuration

All config lives at the top of `linkedin_job_search.py`:

| Variable | Default | Notes |
|----------|---------|-------|
| `SEARCH_TERMS` | 25 terms (embedded, kernel, FPGA, 5G...) | Add/remove job titles |
| `LOCATION` | `"Melbourne, Victoria, Australia"` | Change city |
| `RESULTS_PER_TERM` | `20` | Per job board per term |
| `HOURS_OLD` | `168` (7 days) | Max job age |
| `OUTPUT_DIR` | `"search_results"` | Output directory |

**Job boards used:** LinkedIn, Indeed, **Seek** and **Jora** (the latter two via built-in custom scrapers).

> **Note:** Google for Jobs was removed — it now serves a JS-required shell with no server-rendered
> results, so it returned 0 jobs. PyPI's latest jobspy (1.1.82) has no upstream fix.

Schedule config is managed via the Web UI or by editing `.job_board_schedule.json`:

```json
{
  "enabled": true,
  "mode": "times",
  "times": ["06:00", "22:00"]
}
```

---

## Customisation points

- **Search terms**: Edit `SEARCH_TERMS` in `linkedin_job_search.py` — add your own keywords
- **Signal layers**: Edit `SEMANTIC_DEPTH_LAYERS` to add/remove technical signals
- **IT noise**: Edit `IT_NOISE_PATTERNS` if you're getting false positives from certain tech stacks
- **Clearance rules**: Edit `SECURITY_CLEARANCE_PATTERNS` if your visa situation changes
- **Schedule**: Use the Web UI toggle + time inputs, or edit `.job_board_schedule.json` directly
- **Server IP**: Update the URL in `job_board.py` line 979 and the README if your IP changes

---

## Troubleshooting

**Scheduled search didn't run?**
- The auto-scheduler only works while `job_board.py` is running. Check `ps aux | grep job_board`.
- If it was running, check `job_board.log` for errors.

**No results for a search term?**
- JobSpy rate limits aggressively. Wait a few minutes between runs.
- LinkedIn blocks after ~10 pages without proxies. Indeed is more lenient.
- **Seek** results are scraped from HTML — Seek may block repeated requests; using proxies helps.
- **Jora** is behind Cloudflare and requires a residential IP. It works from this machine but returns
  HTTP 403 ("Just a moment...") from the Oracle VPS, whose datacenter IP scores as suspicious.
  It also ignores server-side date filters, so `HOURS_OLD` is applied client-side in the scraper.

**Too many irrelevant results?**
- Add noise patterns to `IT_NOISE_PATTERNS` in `linkedin_job_search.py`.
- Lower `HOURS_OLD` to narrow the window.
- Increase `MIN_SCORE` (line 721) to raise the relevance threshold.

**Wrong location results?**
- Edit the Melbourne suburb list in `linkedin_job_search.py` line 636-644.
- Set `LOCATION` to your city.

---

## Dependencies

- Python ≥ 3.10
- [python-jobspy](https://github.com/cullenwatson/JobSpy) — multi-board job scraping
- pandas — data wrangling
- Flask — web UI

For full `scrape_jobs()` API reference (proxies, country codes, job types), see the [upstream JobSpy docs](https://github.com/cullenwatson/JobSpy).
