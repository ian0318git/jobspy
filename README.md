# 🔍 Ian's Melbourne Embedded Job Search

Tailored job scraper + Kanban board for embedded systems roles in Melbourne.

> **Forked from [JobSpy](https://github.com/cullenwatson/JobSpy)** by Cullen Watson —
> a multi-board job scraping library. This project adds semantic depth scoring,
> career track classification, security clearance filtering, and a Kanban web UI
> on top of the original `scrape_jobs()` engine.

## What this does

Two scripts:

| Script | Purpose |
|--------|---------|
| `linkedin_job_search.py` | Scrapes LinkedIn/Indeed/Google for embedded jobs, scores them with 3 AI engines, outputs CSV + Kanban JSON |
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
source .venv/bin/activate
python linkedin_job_search.py
```

Output goes to `search_results/`:
```
search_results/
├── melbourne_embedded_jobs_20260629_1435.csv   ← Full results
├── kanban_jobs_20260629_1435.json             ← Top 40 for Kanban board
└── blocked_clearance_jobs_20260629_1435.json  ← Jobs blocked by clearance
```

Each run creates timestamped files — historical results are never overwritten.

### 2. Launch the Kanban board

```bash
source .venv/bin/activate
python job_board.py
```

Open `http://192.168.44.128:5000` in Chrome.

**For the auto-scheduler to work, the server must run persistently:**

```bash
nohup python job_board.py > job_board.log 2>&1 &
```

Idle resource usage: ~0.1% CPU, ~32 MB RAM.

Check it's alive: `ps aux | grep job_board`
Stop it: `pkill -f job_board.py`

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

Auto-detects and blocks jobs requiring NV1/NV2/Baseline clearance or Australian citizenship/PR — unobtainable on a Bridging Visa A.

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
