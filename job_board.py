#!/usr/bin/env python3
"""
Kanban Job Board — Chrome web UI for embedded job search results.

Usage:
    source .venv/bin/activate
    pip install flask
    python job_board.py

    Then open Chrome → http://192.168.44.128:5000

Features:
  - Full-width job cards with compact status controls
  - Status filter tabs (New / Applied / Interview / Offer / Rejected)
  - 📂 Historical file browser — switch between past search results
  - 💾 Cross-file status persistence — Applied/Interview status survives
    across search runs (saved to .job_statuses.json by job URL)
  - 💻 4 job boards: LinkedIn, Indeed, Google, and Seek (via custom scraper)
  - Relevance-tier color coding, embedded depth badges
  - Filter by tier, career track, source + keyword search
  - 🔍 Re-search button — triggers linkedin_job_search.py live
  - ⏰ Auto-scheduler — configurable interval, background thread
"""

import json
import glob
import os
import sys
import subprocess
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request

# ── Config ───────────────────────────────────────────────────────────────────
PORT = 5000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

SCHEDULE_FILE = os.path.join(SCRIPT_DIR, ".job_board_schedule.json")
STATUS_FILE   = os.path.join(SCRIPT_DIR, ".job_statuses.json")

app = Flask(__name__)

# ── Status definitions ───────────────────────────────────────────────────────
STATUSES = [
    {"id": "New",       "emoji": "📥", "label": "New"},
    {"id": "Applied",   "emoji": "📝", "label": "Applied"},
    {"id": "Interview", "emoji": "📞", "label": "Interview"},
    {"id": "Offer",     "emoji": "🎉", "label": "Offer"},
    {"id": "Rejected",  "emoji": "❌", "label": "Rejected"},
]

TIER_COLORS = {
    "🔥🔥 Bare-Metal Gold":      "#fbbf24",
    "🔥 Strong Match":           "#f97316",
    "✅ Good Match":             "#22c55e",
    "⚠️ Possible Match":         "#eab308",
    "🤔 Weak Signal":            "#9ca3af",
    "❌ IT Noise / Irrelevant":  "#6b7280",
}
TIER_ORDER = list(TIER_COLORS.keys())

# ═══════════════════════════════════════════════════════════════════════════════
# Data: kanban files + global status persistence
# ═══════════════════════════════════════════════════════════════════════════════

def list_kanban_files():
    """Return all kanban JSON files sorted newest-first with metadata."""
    files = []
    for subdir in ["search_results", ""]:
        pattern = os.path.join(SCRIPT_DIR, subdir, "kanban_jobs_*.json")
        for f in glob.glob(pattern):
            name = os.path.relpath(f, SCRIPT_DIR)
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            # Parse timestamp from filename like kanban_jobs_20260629_1435.json
            ts_match = os.path.basename(f).replace("kanban_jobs_", "").replace(".json", "")
            try:
                file_ts = datetime.strptime(ts_match, "%Y%m%d_%H%M")
                ts_display = file_ts.strftime("%m/%d %H:%M")
            except ValueError:
                ts_display = mtime.strftime("%m/%d %H:%M")
            files.append({
                "filename": os.path.basename(f),
                "path": name,
                "mtime": mtime.isoformat(),
                "display": f"{ts_display} — {os.path.basename(f)}",
                "ts": ts_display,
            })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def load_global_statuses():
    """Load cross-file status persistence (keyed by job URL)."""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_global_status(url, status, notes=""):
    """Update one entry in the global status file."""
    all_statuses = load_global_statuses()
    all_statuses[url] = {
        "status": status,
        "notes": notes,
        "updated_at": datetime.now().isoformat(),
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(all_statuses, f, indent=2)


def merge_statuses(jobs):
    """Overlay global statuses onto loaded jobs (matched by URL)."""
    gs = load_global_statuses()
    for j in jobs:
        url = j.get("url", "")
        if url in gs:
            j["status"] = gs[url].get("status", "New")
            j["notes"] = gs[url].get("notes", "")
        else:
            j.setdefault("status", "New")
            j.setdefault("notes", "")
    return jobs


def find_latest_kanban():
    files = list_kanban_files()
    if not files:
        return None
    return os.path.join(SCRIPT_DIR, files[0]["path"])


def load_jobs(file_path=None):
    """Load jobs from a specific kanban file, or the latest if None.
    Merges cross-file statuses from .job_statuses.json."""
    if file_path:
        full_path = os.path.join(SCRIPT_DIR, file_path)
        if not os.path.exists(full_path):
            return [], None
    else:
        full_path = find_latest_kanban()
    if not full_path or not os.path.exists(full_path):
        return [], None
    with open(full_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    jobs = merge_statuses(jobs)
    return jobs, os.path.basename(full_path)


def save_jobs_to_kanban(jobs, filename):
    """Save job list back to the current kanban JSON file."""
    path = os.path.join(SCRIPT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


CURRENT_FILE = None  # tracks which file is loaded; set by first load
JOBS, DATA_FILE = load_jobs()
CURRENT_FILE = DATA_FILE


# ═══════════════════════════════════════════════════════════════════════════════
# Background Search
# ═══════════════════════════════════════════════════════════════════════════════
SEARCH_PROCESS = None
SEARCH_OUTPUT = []
SEARCH_START_TIME = None
SEARCH_LOCK = threading.Lock()


def start_search():
    global SEARCH_PROCESS, SEARCH_OUTPUT, SEARCH_START_TIME
    with SEARCH_LOCK:
        if SEARCH_PROCESS and SEARCH_PROCESS.poll() is None:
            return False, "A search is already running"
        SEARCH_OUTPUT = []
        SEARCH_START_TIME = datetime.now().isoformat()
        script = os.path.join(SCRIPT_DIR, "linkedin_job_search.py")
        SEARCH_PROCESS = subprocess.Popen(
            [sys.executable, "-u", script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=SCRIPT_DIR,
        )
        t = threading.Thread(target=_read_search_output, daemon=True)
        t.start()
        return True, "Search started"


def _read_search_output():
    global SEARCH_PROCESS, SEARCH_OUTPUT
    if not SEARCH_PROCESS:
        return
    for line in SEARCH_PROCESS.stdout:
        line = line.rstrip("\n")
        SEARCH_OUTPUT.append((datetime.now().strftime("%H:%M:%S"), line))
    SEARCH_PROCESS.wait()


def get_search_status():
    with SEARCH_LOCK:
        running = SEARCH_PROCESS is not None and SEARCH_PROCESS.poll() is None
        exit_code = SEARCH_PROCESS.poll() if SEARCH_PROCESS else None
        return {
            "running": running,
            "exit_code": exit_code,
            "start_time": SEARCH_START_TIME,
            "output_lines": len(SEARCH_OUTPUT),
            "output": SEARCH_OUTPUT[-80:],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════════
SCHEDULE_DEFAULT = {
    "enabled": False,
    "mode": "times",        # "times" or "interval"
    "interval_hours": 6,
    "times": ["06:00", "22:00"],  # specific times (HH:MM, 24h)
    "last_run": None,
    "next_run": None,
    "last_run_date": None,
    "_fired_today": {},     # {"2026-07-05": ["06:00", "22:00"]}
}


def load_schedule():
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE) as f:
                cfg = json.load(f)
            for k, v in SCHEDULE_DEFAULT.items():
                cfg.setdefault(k, v)
            return cfg
        except (json.JSONDecodeError, IOError):
            pass
    return dict(SCHEDULE_DEFAULT)


def save_schedule(cfg):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


SCHEDULE_CONFIG = load_schedule()
SCHEDULE_STOP = threading.Event()


def scheduler_loop():
    """Background thread: check every 30s if a scheduled search is due."""
    print("[scheduler] Scheduler thread started", flush=True)
    while not SCHEDULE_STOP.is_set():
        try:
            cfg = load_schedule()
            if cfg.get("enabled"):
                now = datetime.now()
                due = False
                mode = cfg.get("mode", "interval")

                if mode == "times":
                    times = cfg.get("times", ["06:00", "22:00"])
                    today_str = now.strftime("%Y-%m-%d")
                    # Track which (date_time) combos have already fired
                    fired_map = cfg.get("_fired_today", {})
                    already_fired = fired_map.get(today_str, [])
                    for t in times:
                        if t in already_fired:
                            continue
                        target = datetime.strptime(f"{today_str} {t}", "%Y-%m-%d %H:%M")
                        # Fire if current time has passed the scheduled time
                        # (within last 6h so a late-waking machine still catches
                        # the morning scan, while avoiding stale catch-up)
                        if now >= target and (now - target).total_seconds() < 21600:
                            due = True
                            break
                else:
                    interval = cfg.get("interval_hours", 6)
                    last = cfg.get("last_run")
                    if last:
                        if now >= datetime.fromisoformat(last) + timedelta(hours=interval):
                            due = True
                    else:
                        due = True

                if due:
                    print(f"[scheduler] Triggering search at {now.strftime('%Y-%m-%d %H:%M')}", flush=True)
                    ok, _ = start_search()
                    if ok:
                        while True:
                            st = get_search_status()
                            if not st["running"]:
                                break
                            time.sleep(5)
                        global JOBS, DATA_FILE, CURRENT_FILE
                        JOBS, DATA_FILE = load_jobs()
                        CURRENT_FILE = DATA_FILE
                        now2 = datetime.now()
                        cfg["last_run"] = now2.isoformat()
                        cfg["last_run_date"] = now2.strftime("%Y-%m-%d")
                        if mode == "times":
                            fired_map = cfg.get("_fired_today", {})
                            today_str = now2.strftime("%Y-%m-%d")
                            if today_str not in fired_map:
                                fired_map[today_str] = []
                            # Record the time that just fired
                            fired_time = now2.strftime("%H:%M")
                            for t in times:
                                target = datetime.strptime(f"{today_str} {t}", "%Y-%m-%d %H:%M")
                                if abs((now2 - target).total_seconds()) < 21600:
                                    if t not in fired_map[today_str]:
                                        fired_map[today_str].append(t)
                                    break
                            cfg["_fired_today"] = fired_map
                            # Determine next run time
                            future = [t for t in times if t > now2.strftime("%H:%M")]
                            if future:
                                next_t = future[0]
                                next_date = now2.strftime("%Y-%m-%d")
                            else:
                                # All times passed -> next run is tomorrow's first slot
                                next_t = times[0]
                                next_date = (now2 + timedelta(days=1)).strftime("%Y-%m-%d")
                            cfg["next_run"] = f"{next_date} {next_t}"
                            # Clean up entries older than 3 days
                            old_dates = [d for d in fired_map if d < (now2 - timedelta(days=3)).strftime("%Y-%m-%d")]
                            for d in old_dates:
                                del fired_map[d]
                        else:
                            cfg["next_run"] = (now2 + timedelta(hours=cfg.get("interval_hours", 6))).isoformat()
                        save_schedule(cfg)
        except Exception as e:
            print(f"[scheduler] Error: {e}", flush=True)
        SCHEDULE_STOP.wait(30)


_scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
_scheduler_thread.start()


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return _HTML


@app.route("/api/files")
def api_files():
    return jsonify(list_kanban_files())


@app.route("/api/jobs")
def api_jobs():
    global JOBS, DATA_FILE, CURRENT_FILE
    file_param = request.args.get("file")
    if file_param:
        JOBS, DATA_FILE = load_jobs(file_param)
        CURRENT_FILE = DATA_FILE
    return jsonify({
        "data_file": CURRENT_FILE or DATA_FILE,
        "current_file": CURRENT_FILE or DATA_FILE,
        "statuses": STATUSES,
        "tier_order": TIER_ORDER,
        "tier_colors": TIER_COLORS,
        "jobs": JOBS,
        "stats": compute_stats(),
    })


@app.route("/api/jobs/<int:job_index>", methods=["PATCH"])
def api_update_job(job_index):
    global JOBS
    if job_index < 0 or job_index >= len(JOBS):
        return jsonify({"error": "Invalid job index"}), 404
    data = request.get_json()
    new_status = data.get("status", JOBS[job_index].get("status", "New"))
    new_notes = data.get("notes", JOBS[job_index].get("notes", ""))
    JOBS[job_index]["status"] = new_status
    JOBS[job_index]["notes"] = new_notes
    # Persist to BOTH the kanban file AND the global status store
    save_jobs_to_kanban(JOBS, CURRENT_FILE or DATA_FILE)
    url = JOBS[job_index].get("url", "")
    if url:
        save_global_status(url, new_status, new_notes)
    return jsonify({"ok": True, "job": JOBS[job_index]})


@app.route("/api/reload")
def api_reload():
    global JOBS, DATA_FILE, CURRENT_FILE
    JOBS, DATA_FILE = load_jobs(CURRENT_FILE)
    CURRENT_FILE = DATA_FILE
    return jsonify({"ok": True, "data_file": CURRENT_FILE, "count": len(JOBS)})


@app.route("/api/search", methods=["POST"])
def api_search():
    ok, msg = start_search()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/search/status")
def api_search_status():
    return jsonify(get_search_status())


@app.route("/api/schedule", methods=["GET", "POST"])
def api_schedule():
    global SCHEDULE_CONFIG
    if request.method == "POST":
        data = request.get_json()
        if "enabled" in data:
            SCHEDULE_CONFIG["enabled"] = bool(data["enabled"])
        if "mode" in data:
            SCHEDULE_CONFIG["mode"] = data["mode"]
            SCHEDULE_CONFIG.pop("_fired_today", None)  # reset tracking on mode change
        if "interval_hours" in data:
            SCHEDULE_CONFIG["interval_hours"] = int(data["interval_hours"])
        if "times" in data:
            SCHEDULE_CONFIG["times"] = data["times"]
            SCHEDULE_CONFIG.pop("_fired_today", None)  # reset tracking on time change
        if SCHEDULE_CONFIG["enabled"]:
            if SCHEDULE_CONFIG.get("mode") == "times":
                times = SCHEDULE_CONFIG.get("times", ["06:00", "22:00"])
                now = datetime.now()
                now_str = now.strftime("%H:%M")
                future = [t for t in sorted(times) if t > now_str]
                if future:
                    next_t = future[0]
                    next_date = now.strftime("%Y-%m-%d")
                else:
                    # All times passed -> next run is tomorrow's first slot
                    next_t = sorted(times)[0]
                    next_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                SCHEDULE_CONFIG["next_run"] = f"{next_date} {next_t}"
            else:
                last = SCHEDULE_CONFIG.get("last_run")
                base = datetime.fromisoformat(last) if last else datetime.now()
                SCHEDULE_CONFIG["next_run"] = (base + timedelta(hours=SCHEDULE_CONFIG["interval_hours"])).isoformat()
        else:
            SCHEDULE_CONFIG["next_run"] = None
        save_schedule(SCHEDULE_CONFIG)
        return jsonify({"ok": True, "schedule": SCHEDULE_CONFIG})
    SCHEDULE_CONFIG = load_schedule()
    return jsonify(SCHEDULE_CONFIG)


def compute_stats():
    tiers, tracks, sources, columns = {}, {}, {}, {}
    for j in JOBS:
        t = j.get("relevance", "Unknown"); tiers[t] = tiers.get(t, 0) + 1
        tk = j.get("career_track", "Unknown"); tracks[tk] = tracks.get(tk, 0) + 1
        s = j.get("source", "Unknown"); sources[s] = sources.get(s, 0) + 1
        c = j.get("status", "New"); columns[c] = columns.get(c, 0) + 1
    return {"total": len(JOBS), "by_tier": tiers, "by_track": tracks, "by_source": sources, "by_column": columns}


# ═══════════════════════════════════════════════════════════════════════════════
# HTML / CSS / JS
# ═══════════════════════════════════════════════════════════════════════════════

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔍 Embedded Job Board</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  background:#0f172a;color:#e2e8f0;min-height:100vh;font-size:16px;line-height:1.5;
}

/* ── Header ──────────────────────────────────────────────────────────── */
.header{
  background:#1e293b;border-bottom:2px solid #334155;
  padding:14px 28px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;gap:16px;flex-wrap:wrap;
}
.header h1{font-size:1.3rem;font-weight:700;}
.header .meta{font-size:0.85rem;color:#94a3b8;display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.header .meta code{color:#fbbf24;font-size:0.8rem;}
.header .meta select{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:4px 8px;border-radius:6px;font-size:0.78rem;cursor:pointer;max-width:280px;
}
.header .btn-row{display:flex;gap:8px;align-items:center;}

/* ── Buttons ─────────────────────────────────────────────────────────── */
.btn{
  background:#334155;color:#e2e8f0;border:1px solid #475569;
  padding:7px 18px;border-radius:8px;cursor:pointer;font-size:0.85rem;
  transition:all .15s;white-space:nowrap;font-weight:500;
  display:inline-flex;align-items:center;gap:5px;
}
.btn:hover{background:#475569;}
.btn.primary{background:#6366f1;border-color:#6366f1;color:#fff;}
.btn.primary:hover{background:#4f46e5;}
.btn:disabled{opacity:0.4;cursor:not-allowed;}

/* ── Panel (collapsible) ─────────────────────────────────────────────── */
.panel{
  background:#1a2332;border-bottom:1px solid #334155;
  padding:0 28px;overflow:hidden;transition:max-height .3s;max-height:0;
}
.panel.open{max-height:500px;padding:16px 28px;}
.panel-title{font-size:0.9rem;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px;}
.panel .console{
  background:#0a0f1a;color:#86efac;border:1px solid #1e3a5f;border-radius:8px;
  padding:12px 16px;font-family:'SF Mono','Fira Code','Cascadia Code',monospace;
  font-size:0.75rem;line-height:1.6;max-height:280px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;
}
.panel .schedule-row{
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;
}
.panel .schedule-row label{font-size:0.85rem;color:#94a3b8;}
.panel .schedule-row select{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:6px 12px;border-radius:8px;font-size:0.85rem;
}

/* Toggle */
.toggle{position:relative;display:inline-block;width:48px;height:26px;}
.toggle input{opacity:0;width:0;height:0;}
.toggle .slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#334155;border-radius:26px;transition:.2s;}
.toggle .slider::before{content:"";position:absolute;height:20px;width:20px;left:3px;bottom:3px;background:#94a3b8;border-radius:50%;transition:.2s;}
.toggle input:checked+.slider{background:#6366f1;}
.toggle input:checked+.slider::before{transform:translateX(22px);background:#fff;}

/* ── Toolbar ─────────────────────────────────────────────────────────── */
.toolbar{
  display:flex;gap:12px;padding:12px 28px;background:#1a2332;
  border-bottom:1px solid #334155;flex-wrap:wrap;align-items:center;
}
.toolbar label{font-size:0.8rem;color:#94a3b8;white-space:nowrap;}
.toolbar select,.toolbar input{
  background:#0f172a;color:#e2e8f0;border:1px solid #475569;
  padding:7px 12px;border-radius:8px;font-size:0.85rem;
}
.toolbar input{flex:1;min-width:200px;max-width:340px;}
.toolbar select{cursor:pointer;}
.toolbar .count{font-size:0.8rem;color:#64748b;margin-left:auto;}

/* ── Status tabs ─────────────────────────────────────────────────────── */
.status-tabs{
  display:flex;gap:6px;padding:12px 28px;background:#1a2332;
  border-bottom:1px solid #334155;flex-wrap:wrap;
}
.status-tab{
  padding:8px 18px;border-radius:20px;border:1px solid #475569;
  background:#0f172a;color:#94a3b8;cursor:pointer;font-size:0.85rem;
  transition:all .15s;white-space:nowrap;font-weight:500;
}
.status-tab:hover{background:#1e293b;color:#e2e8f0;}
.status-tab.active{background:#6366f1;border-color:#6366f1;color:#fff;}
.status-tab .badge{
  display:inline-block;background:#00000030;color:inherit;
  padding:1px 9px;border-radius:10px;font-size:0.75rem;margin-left:4px;
}

/* ── Stats bar ───────────────────────────────────────────────────────── */
.stats-bar{
  display:flex;gap:18px;padding:10px 28px;background:#151d2a;
  border-bottom:1px solid #1e293b;flex-wrap:wrap;font-size:0.8rem;color:#94a3b8;
}
.stats-bar strong{color:#e2e8f0;}
.stats-bar .sched-info{color:#fbbf24;margin-left:auto;font-size:0.78rem;}

/* ── Card list ───────────────────────────────────────────────────────── */
.card-list{padding:16px 28px;display:flex;flex-direction:column;gap:14px;}
.card{
  background:#1e293b;border:1px solid #334155;border-radius:12px;
  padding:18px 22px;transition:border-color .15s;display:flex;flex-direction:column;gap:10px;
}
.card:hover{border-color:#6366f1;}
.card.highlight{border-left:4px solid var(--tier-color,#6366f1);}
.card .row1{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
.card .tier-badge{
  font-size:0.75rem;font-weight:700;padding:3px 10px;border-radius:5px;white-space:nowrap;flex-shrink:0;
}
.card .job-title{font-size:1.1rem;font-weight:600;color:#f1f5f9;}
.card .company-name{font-size:0.9rem;color:#94a3b8;white-space:nowrap;}
.card .row2{display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem;color:#94a3b8;}
.card .row2 span{display:flex;align-items:center;gap:4px;}
.card .row3{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.card .tag{font-size:0.72rem;padding:3px 9px;border-radius:4px;white-space:nowrap;font-weight:500;}
.tag-tier{background:#1e3a5f;color:#93c5fd;}
.tag-score{background:#3b1f1f;color:#fca5a5;}
.tag-purity{background:#1e2a1e;color:#86efac;}
.tag-track-ic{background:#064e3b;color:#6ee7b7;}
.tag-track-lead{background:#4a1d96;color:#c4b5fd;}
.tag-track-hybrid{background:#1e3a5f;color:#93c5fd;}
.tag-clearance-ok{background:#064e3b;color:#6ee7b7;}
.tag-clearance-blocked{background:#4a1d1d;color:#fca5a5;}
.tag-kw{background:#1e293b;color:#cbd5e1;border:1px solid #334155;}
.card .row4{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between;}
.card .row4 .left{display:flex;gap:8px;align-items:center;}
.card .url-btn{
  display:inline-flex;align-items:center;gap:4px;
  color:#818cf8;text-decoration:none;font-size:0.85rem;font-weight:500;
  padding:6px 12px;border-radius:6px;border:1px solid #818cf840;transition:background .15s;
}
.card .url-btn:hover{background:#818cf815;}
.card .status-btns{display:flex;gap:5px;flex-wrap:wrap;}
.card .status-btn{
  font-size:0.75rem;padding:6px 14px;border-radius:6px;
  border:1px solid #475569;background:#0f172a;color:#94a3b8;
  cursor:pointer;transition:all .15s;font-weight:500;
}
.card .status-btn:hover{background:#334155;color:#e2e8f0;border-color:#6366f1;}
.card .current-status{font-size:0.75rem;padding:6px 14px;border-radius:6px;font-weight:600;white-space:nowrap;}

/* ── Misc ────────────────────────────────────────────────────────────── */
.empty-state{text-align:center;padding:60px 20px;color:#475569;}
.empty-state .emoji{font-size:3rem;margin-bottom:12px;}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{
  display:inline-block;width:16px;height:16px;border:2px solid #475569;
  border-top-color:#818cf8;border-radius:50%;animation:spin .8s linear infinite;
  vertical-align:middle;margin-right:4px;
}
@media(max-width:700px){
  .header,.toolbar,.status-tabs,.stats-bar,.card-list,.panel{padding-left:14px;padding-right:14px;}
  .card{padding:14px 16px;}.card .job-title{font-size:1rem;}
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 Embedded Job Board</h1>
    <div class="meta">
      📂 <select id="file-selector" onchange="switchFile(this.value)"><option>Loading...</option></select>
      <span id="job-count" style="font-size:0.75rem;color:#64748b;">—</span>
      <span id="sched-badge" style="display:none;margin-left:8px;font-size:0.75rem;color:#fbbf24;">⏰ Auto</span>
    </div>
  </div>
  <div class="btn-row">
    <button class="btn" onclick="reloadCurrent()">🔄 Reload</button>
    <button class="btn primary" id="btn-search" onclick="triggerSearch()">🔍 Re-Search</button>
    <button class="btn" id="btn-schedule" onclick="toggleSchedPanel()">⏰ Schedule</button>
  </div>
</div>

<!-- Search console -->
<div class="panel" id="search-panel">
  <div class="panel-title">
    <span id="search-status-icon">🔍</span>
    <span id="search-status-text">Search not running</span>
    <span style="font-size:0.7rem;color:#64748b;" id="search-elapsed"></span>
  </div>
  <div class="console" id="search-console">Click 🔍 Re-Search to start a new job scan.</div>
</div>

<!-- Schedule -->
<div class="panel" id="sched-panel">
  <div class="panel-title">⏰ Auto-Search Schedule</div>
  <div class="schedule-row">
    <label class="toggle">
      <input type="checkbox" id="sched-enabled" onchange="updateSchedule()"><span class="slider"></span>
    </label>
    <label for="sched-enabled" style="cursor:pointer;">Enable scheduled auto-search</label>
  </div>
  <div class="schedule-row" style="margin-top:8px;">
    <label>Mode:</label>
    <select id="sched-mode" onchange="onSchedModeChange()">
      <option value="times" selected>Specific times</option>
      <option value="interval">Every N hours</option>
    </select>
    <span id="sched-times-group">
      <label style="margin-left:8px;">At:</label>
      <input type="time" id="sched-time1" value="06:00" onchange="updateSchedule()" style="background:#0f172a;color:#e2e8f0;border:1px solid #475569;padding:6px 10px;border-radius:8px;font-size:0.85rem;">
      <input type="time" id="sched-time2" value="22:00" onchange="updateSchedule()" style="background:#0f172a;color:#e2e8f0;border:1px solid #475569;padding:6px 10px;border-radius:8px;font-size:0.85rem;">
    </span>
    <span id="sched-interval-group" style="display:none;">
      <label style="margin-left:8px;">Every</label>
      <select id="sched-interval" onchange="updateSchedule()">
        <option value="3">3 hours</option><option value="6" selected>6 hours</option>
        <option value="12">12 hours</option><option value="24">24 hours</option>
      </select>
    </span>
    <span style="font-size:0.8rem;color:#64748b;" id="sched-next-run"></span>
  </div>
</div>

<div class="toolbar">
  <label>Filter:</label>
  <select id="filter-tier" onchange="renderAll()"><option value="">All Tiers</option></select>
  <select id="filter-track" onchange="renderAll()"><option value="">All Tracks</option></select>
  <select id="filter-source" onchange="renderAll()"><option value="">All Sources</option></select>
  <input type="text" id="filter-search" placeholder="🔍 Search title, company, keywords..." oninput="renderAll()">
  <span class="count" id="filter-count"></span>
</div>

<div class="status-tabs" id="status-tabs"></div>
<div class="stats-bar" id="stats-bar"></div>
<div class="card-list" id="card-list"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let JOBS=[], CURRENT_FILE='', STATUSES=[], TIER_COLORS={}, TIER_ORDER=[];
let activeStatus='All', searchPollTimer=null, FILES=[];

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
async function init(){
  await loadFileList();
  await loadData();
  loadSchedule();
}

async function loadFileList(){
  try{
    const r=await fetch('/api/files'); FILES=await r.json();
    const sel=document.getElementById('file-selector');
    sel.innerHTML=FILES.map((f,i)=>`<option value="${escHtml(f.path)}"${i===0?' selected':''}>${escHtml(f.display)}</option>`).join('');
  }catch(e){console.error(e);}
}

async function loadData(filePath){
  let url='/api/jobs';
  if(filePath) url+='?file='+encodeURIComponent(filePath);
  const r=await fetch(url), d=await r.json();
  JOBS=d.jobs; CURRENT_FILE=d.current_file||d.data_file; STATUSES=d.statuses;
  TIER_COLORS=d.tier_colors; TIER_ORDER=d.tier_order;
  document.getElementById('job-count').textContent=JOBS.length+' jobs';
  // Sync file selector
  const sel=document.getElementById('file-selector');
  for(let i=0;i<sel.options.length;i++){
    if(sel.options[i].value===CURRENT_FILE||FILES[i]&&FILES[i].filename===CURRENT_FILE){
      sel.value=sel.options[i].value; break;
    }
  }
  populateFilters(); renderAll();
}

// ═══════════════════════════════════════════════════════════════════════════
// File switching
// ═══════════════════════════════════════════════════════════════════════════
async function switchFile(path){
  if(!path) return;
  await loadData(path);
}

async function reloadCurrent(){
  try{
    await fetch('/api/reload');
    await loadFileList();
    await loadData(CURRENT_FILE);
  }catch(e){console.error(e);}
}

function populateFilters(){
  const byId=id=>document.getElementById(id);
  [{sel:'filter-tier',vals:TIER_ORDER},
   {sel:'filter-track',vals:[...new Set(JOBS.map(j=>j.career_track))].sort()},
   {sel:'filter-source',vals:[...new Set(JOBS.map(j=>j.source))].sort()}
  ].forEach(({sel,vals})=>{
    const el=byId(sel), cur=el.value;
    el.innerHTML='<option value="">'+({['filter-tier']:'All Tiers',['filter-track']:'All Tracks',['filter-source']:'All Sources'}[sel])+'</option>';
    vals.filter(Boolean).forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);});
    el.value=cur;
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Filtering
// ═══════════════════════════════════════════════════════════════════════════
function getFiltered(){
  const tier=document.getElementById('filter-tier').value,
        track=document.getElementById('filter-track').value,
        source=document.getElementById('filter-source').value,
        search=document.getElementById('filter-search').value.toLowerCase();
  return JOBS.filter(j=>{
    if(tier && j.relevance!==tier) return false;
    if(track && j.career_track!==track) return false;
    if(source && j.source!==source) return false;
    if(activeStatus!=='All' && (j.status||'New')!==activeStatus) return false;
    if(search){
      const h=`${j.title} ${j.company} ${j.matched_on} ${j.track_detail} ${j.notes}`.toLowerCase();
      if(!h.includes(search)) return false;
    }
    return true;
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════════════
function renderAll(){renderStatusTabs();renderStats();renderCards();}

function renderStatusTabs(){
  const bar=document.getElementById('status-tabs');
  let counts={};
  JOBS.forEach(j=>{const s=j.status||'New'; counts[s]=(counts[s]||0)+1;});
  let html=`<div class="status-tab${activeStatus==='All'?' active':''}" onclick="setStatusFilter('All')">📋 All<span class="badge">${JOBS.length}</span></div>`;
  STATUSES.forEach(st=>{
    const c=counts[st.id]||0;
    html+=`<div class="status-tab${activeStatus===st.id?' active':''}" onclick="setStatusFilter('${st.id}')">${st.emoji} ${st.label}<span class="badge">${c}</span></div>`;
  });
  bar.innerHTML=html;
}

function setStatusFilter(s){activeStatus=s;renderAll();}

function renderStats(){
  const bar=document.getElementById('stats-bar');
  const filtered=getFiltered();
  let cols={}; filtered.forEach(j=>{const s=j.status||'New'; cols[s]=(cols[s]||0)+1;});
  bar.innerHTML=`Showing <strong>${filtered.length}</strong> of <strong>${JOBS.length}</strong> jobs`+
    STATUSES.map(st=>` · ${st.emoji} <strong>${cols[st.id]||0}</strong> ${st.label}`).join('')+
    `<span class="sched-info" id="sched-info"></span>`;
}

function renderCards(){
  const list=document.getElementById('card-list');
  const filtered=getFiltered();
  document.getElementById('filter-count').textContent=`${filtered.length} of ${JOBS.length}`;
  if(!filtered.length){
    list.innerHTML=`<div class="empty-state"><div class="emoji">📭</div><p>No jobs match the current filters</p></div>`;
    return;
  }
  let html='';
  filtered.forEach(j=>{
    const idx=JOBS.indexOf(j);
    const tierColor=TIER_COLORS[j.relevance]||'#6b7280';
    const status=j.status||'New';
    const purityPct=j.purity!=null?Math.round(j.purity*100)+'%':'';
    const trackClass=j.career_track&&j.career_track.includes('Lead')?'tag-track-lead'
                   :j.career_track&&j.career_track.includes('Hybrid')?'tag-track-hybrid':'tag-track-ic';
    const blocked=j.clearance_status&&j.clearance_status.includes('BLOCKED');
    let statusBtns='';
    STATUSES.forEach(st=>{
      if(st.id===status){
        statusBtns+=`<span class="current-status" style="background:var(--btn-color,#6366f1);color:#fff;">${st.emoji} ${st.label}</span>`;
      }else{
        statusBtns+=`<button class="status-btn" style="--btn-color:${st.id==='New'?'#6366f1':st.id==='Applied'?'#f59e0b':st.id==='Interview'?'#8b5cf6':st.id==='Offer'?'#10b981':'#6b7280'}" onclick="moveJob(${idx},'${st.id}')">${st.emoji} ${st.label}</button>`;
      }
    });
    let kwTags='';
    if(j.matched_on){
      j.matched_on.split(', ').slice(0,5).forEach(k=>{kwTags+=`<span class="tag tag-kw">#${escHtml(k)}</span>`;});
    }
    html+=`
    <div class="card highlight" style="--tier-color:${tierColor}">
      <div class="row1">
        <span class="tier-badge" style="background:${tierColor}20;color:${tierColor};border:1px solid ${tierColor}40">${escHtml(j.relevance||'?')}</span>
        <span class="job-title">${escHtml(j.title)}</span>
        <span class="company-name">🏢 ${escHtml(j.company)}</span>
      </div>
      <div class="row2">
        <span>📍 ${escHtml(j.location)}</span><span>📅 ${j.date_posted||'?'}</span><span>📎 ${escHtml(j.source||'?')}</span>
        ${j.clearance_status?`<span class="${blocked?'tag-clearance-blocked':'tag-clearance-ok'}" style="font-size:0.75rem;padding:2px 8px;border-radius:4px;">${escHtml(j.clearance_status)}</span>`:''}
      </div>
      <div class="row3">
        ${j.embedded_tier?`<span class="tag tag-tier">🏷 ${escHtml(j.embedded_tier)}</span>`:''}
        ${j.score?`<span class="tag tag-score">⭐ ${j.score}</span>`:''}
        ${purityPct?`<span class="tag tag-purity">🧪 ${purityPct}</span>`:''}
        ${j.career_track?`<span class="tag ${trackClass}">${escHtml(j.career_track)}</span>`:''}
        ${j.track_detail?`<span class="tag tag-tier">${escHtml(j.track_detail)}</span>`:''}
        ${kwTags}
        ${j.matched_on&&j.matched_on.split(', ').length>5?`<span class="tag tag-kw">+${j.matched_on.split(', ').length-5} more</span>`:''}
      </div>
      <div class="row4">
        <div class="left">
          <a class="url-btn" href="${escHtml(j.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">🔗 View on ${escHtml(j.source||'site')} →</a>
          ${j.noise_warning?`<span style="font-size:0.7rem;color:#f87171;">⚠️ Noise: ${escHtml(j.noise_warning)}</span>`:''}
        </div>
        <div class="status-btns">${statusBtns}</div>
      </div>
    </div>`;
  });
  list.innerHTML=html;
}

function escHtml(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// ═══════════════════════════════════════════════════════════════════════════
// Job actions
// ═══════════════════════════════════════════════════════════════════════════
async function moveJob(idx,newStatus){
  try{
    const r=await fetch('/api/jobs/'+idx,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:newStatus})});
    if(r.ok){const d=await r.json();JOBS[idx].status=d.job.status;renderAll();}
  }catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Search
// ═══════════════════════════════════════════════════════════════════════════
async function triggerSearch(){
  const btn=document.getElementById('btn-search'), panel=document.getElementById('search-panel');
  const cel=document.getElementById('search-console');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Starting...';
  panel.classList.add('open'); cel.textContent='Starting search...\n';
  document.getElementById('search-status-text').textContent='Starting...';
  try{
    const r=await fetch('/api/search',{method:'POST'}), d=await r.json();
    if(!d.ok){cel.textContent+='⚠️ '+d.message+'\n';btn.disabled=false;btn.innerHTML='🔍 Re-Search';return;}
    cel.textContent+='✅ '+d.message+'\n'; startPolling();
  }catch(e){cel.textContent+='❌ Error: '+e+'\n';btn.disabled=false;btn.innerHTML='🔍 Re-Search';}
}

function startPolling(){
  if(searchPollTimer) clearInterval(searchPollTimer);
  searchPollTimer=setInterval(pollSearchStatus,1500); pollSearchStatus();
}

async function pollSearchStatus(){
  try{
    const r=await fetch('/api/search/status'), s=await r.json();
    const cel=document.getElementById('search-console'), panel=document.getElementById('search-panel');
    const icon=document.getElementById('search-status-icon'), txt=document.getElementById('search-status-text');
    const elapsed=document.getElementById('search-elapsed'), btn=document.getElementById('btn-search');
    if(s.output&&s.output.length>0){
      const cur=cel.textContent.split('\n').filter(Boolean);
      s.output.forEach(l=>{if(!cur.includes(l[1])) cel.textContent+=`[${l[0]}] ${l[1]}\n`;});
      cel.scrollTop=cel.scrollHeight;
    }
    if(s.running){
      icon.textContent='⏳'; txt.textContent='Search running...';
      if(s.start_time){const secs=Math.floor((Date.now()-new Date(s.start_time).getTime())/1000);elapsed.textContent=`(${Math.floor(secs/60)}m ${secs%60}s)`;}
      btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Running...'; panel.classList.add('open');
    }else{
      if(searchPollTimer){clearInterval(searchPollTimer);searchPollTimer=null;}
      if(s.exit_code===0){
        icon.textContent='✅'; txt.textContent='Search completed!';
        cel.textContent+='\n✅ Search finished. Reloading...\n';
        await loadFileList();
        await loadData();
        cel.textContent+='✅ Data reloaded!\n';
      }else if(s.exit_code!==null){
        icon.textContent='❌'; txt.textContent='Search failed (exit '+s.exit_code+')';
      }
      elapsed.textContent=''; btn.disabled=false; btn.innerHTML='🔍 Re-Search';
    }
  }catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Schedule
// ═══════════════════════════════════════════════════════════════════════════
function toggleSchedPanel(){document.getElementById('sched-panel').classList.toggle('open');}

function onSchedModeChange(){
  const mode=document.getElementById('sched-mode').value;
  document.getElementById('sched-times-group').style.display=mode==='times'?'':'none';
  document.getElementById('sched-interval-group').style.display=mode==='interval'?'':'none';
  updateSchedule();
}

async function loadSchedule(){
  try{
    const r=await fetch('/api/schedule'), s=await r.json();
    document.getElementById('sched-enabled').checked=s.enabled;
    document.getElementById('sched-mode').value=s.mode||'times';
    document.getElementById('sched-interval').value=s.interval_hours||6;
    const times=s.times||['06:00','22:00'];
    document.getElementById('sched-time1').value=times[0]||'06:00';
    document.getElementById('sched-time2').value=times[1]||'22:00';
    onSchedModeChange();
    updateSchedDisplay(s);
  }catch(e){console.error(e);}
}

async function updateSchedule(){
  const mode=document.getElementById('sched-mode').value;
  const enabled=document.getElementById('sched-enabled').checked;
  const interval_hours=parseInt(document.getElementById('sched-interval').value);
  const t1=document.getElementById('sched-time1').value;
  const t2=document.getElementById('sched-time2').value;
  const times=[t1,t2].filter(Boolean).sort();
  try{
    const r=await fetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,mode,interval_hours,times})});
    const s=await r.json(); updateSchedDisplay(s.schedule||s);
  }catch(e){console.error(e);}
}

function updateSchedDisplay(s){
  const badge=document.getElementById('sched-badge'), info=document.getElementById('sched-info');
  const nr=document.getElementById('sched-next-run');
  if(s.enabled){
    badge.style.display='inline';
    const mode=s.mode||'times';
    let t=mode==='times'
      ?`Auto: at ${(s.times||['06:00','22:00']).join(' & ')} daily`
      :`Auto: every ${s.interval_hours}h`;
    if(s.next_run){const d=new Date(s.next_run);t+=` · Next: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;}
    if(s.last_run){const d=new Date(s.last_run);t+=` · Last: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;}
    info.textContent=t;
  }else{badge.style.display='none';info.textContent='';}
  nr.textContent=s.next_run&&s.enabled?`Next run: ${new Date(s.next_run).toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`:'';
}

// Keyboard
document.addEventListener('keydown',e=>{if(e.key==='r'&&e.ctrlKey){e.preventDefault();reloadCurrent();}});

// Boot
init();
</script>
</body>
</html>"""

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("🔍 Embedded Job Board")
    print(f"   Data : {DATA_FILE or '(no kanban JSON)'}")
    print(f"   URL  : http://192.168.44.128:{PORT}")
    print(f"   Sched: {'ON' if SCHEDULE_CONFIG.get('enabled') else 'OFF'} "
          f"(every {SCHEDULE_CONFIG.get('interval_hours', 6)}h)")
    print("=" * 60)
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False)
    finally:
        SCHEDULE_STOP.set()
