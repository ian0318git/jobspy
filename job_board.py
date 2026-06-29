#!/usr/bin/env python3
"""
Kanban Job Board — Chrome web UI for Ian's embedded job search results.

Usage:
    source .venv/bin/activate
    pip install flask
    python job_board.py

    Then open Chrome → http://192.168.44.128:5000

Features:
  - Full-width job cards with compact status controls
  - Status filter tabs (New / Applied / Interview / Offer / Rejected)
  - Relevance-tier color coding, embedded depth badges
  - Filter by tier, career track, source + keyword search
  - One-click status updates, auto-persist to JSON
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

from flask import Flask, jsonify, request, Response

# ── Config ───────────────────────────────────────────────────────────────────
PORT = 5000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

SCHEDULE_FILE = os.path.join(SCRIPT_DIR, ".job_board_schedule.json")

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

# ── Data loading ─────────────────────────────────────────────────────────────

def find_latest_kanban():
    # Search in search_results/ folder first, then fall back to root
    for subdir in ["search_results", ""]:
        pattern = os.path.join(SCRIPT_DIR, subdir, "kanban_jobs_*.json")
        files = glob.glob(pattern)
        if files:
            return max(files, key=os.path.getmtime)
    return None


def load_jobs():
    path = find_latest_kanban()
    if not path:
        return [], None
    with open(path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    for j in jobs:
        j.setdefault("status", "New")
        j.setdefault("notes", "")
    return jobs, os.path.basename(path)


def save_jobs(jobs, filename):
    path = os.path.join(SCRIPT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


JOBS, DATA_FILE = load_jobs()

# ── Background Search ────────────────────────────────────────────────────────
SEARCH_PROCESS = None
SEARCH_OUTPUT = []       # list of (timestamp, line)
SEARCH_START_TIME = None
SEARCH_LOCK = threading.Lock()


def start_search():
    """Launch linkedin_job_search.py as a subprocess. Non-blocking."""
    global SEARCH_PROCESS, SEARCH_OUTPUT, SEARCH_START_TIME
    with SEARCH_LOCK:
        if SEARCH_PROCESS and SEARCH_PROCESS.poll() is None:
            return False, "A search is already running"
        SEARCH_OUTPUT = []
        SEARCH_START_TIME = datetime.now().isoformat()
        script = os.path.join(SCRIPT_DIR, "linkedin_job_search.py")
        SEARCH_PROCESS = subprocess.Popen(
            [sys.executable, "-u", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=SCRIPT_DIR,
        )
        # Start a reader thread
        t = threading.Thread(target=_read_search_output, daemon=True)
        t.start()
        return True, "Search started"


def _read_search_output():
    """Read subprocess output line by line into SEARCH_OUTPUT."""
    global SEARCH_PROCESS, SEARCH_OUTPUT
    if not SEARCH_PROCESS:
        return
    for line in SEARCH_PROCESS.stdout:
        line = line.rstrip("\n")
        SEARCH_OUTPUT.append((datetime.now().strftime("%H:%M:%S"), line))
    SEARCH_PROCESS.wait()


def get_search_status():
    """Return current search state."""
    with SEARCH_LOCK:
        running = SEARCH_PROCESS is not None and SEARCH_PROCESS.poll() is None
        exit_code = SEARCH_PROCESS.poll() if SEARCH_PROCESS else None
        return {
            "running": running,
            "exit_code": exit_code,
            "start_time": SEARCH_START_TIME,
            "output_lines": len(SEARCH_OUTPUT),
            "output": SEARCH_OUTPUT[-80:],  # last 80 lines
        }


# ── Scheduler ────────────────────────────────────────────────────────────────
SCHEDULE_DEFAULT = {
    "enabled": False,
    "interval_hours": 6,
    "last_run": None,
    "next_run": None,
}


def load_schedule():
    """Load schedule config from disk."""
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r") as f:
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
    """Background thread: check every 60s if a scheduled search is due."""
    while not SCHEDULE_STOP.is_set():
        cfg = load_schedule()
        if cfg.get("enabled"):
            interval = cfg.get("interval_hours", 6)
            last = cfg.get("last_run")
            now = datetime.now()
            due = False
            if last:
                last_dt = datetime.fromisoformat(last)
                if now >= last_dt + timedelta(hours=interval):
                    due = True
            else:
                due = True  # never run — run immediately
            if due:
                print(f"[scheduler] Triggering scheduled search at {now.strftime('%Y-%m-%d %H:%M')}")
                ok, _ = start_search()
                if ok:
                    # Wait for search to finish
                    while True:
                        st = get_search_status()
                        if not st["running"]:
                            break
                        time.sleep(5)
                    # Reload jobs
                    global JOBS, DATA_FILE
                    JOBS, DATA_FILE = load_jobs()
                    cfg["last_run"] = datetime.now().isoformat()
                    cfg["next_run"] = (datetime.now() + timedelta(hours=interval)).isoformat()
                    save_schedule(cfg)
        # Check every 60 seconds
        SCHEDULE_STOP.wait(60)


# Start scheduler thread
_scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
_scheduler_thread.start()

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return _HTML


@app.route("/api/jobs")
def api_jobs():
    return jsonify({
        "data_file": DATA_FILE,
        "statuses": STATUSES,
        "tier_order": TIER_ORDER,
        "tier_colors": TIER_COLORS,
        "jobs": JOBS,
        "stats": compute_stats(),
    })


@app.route("/api/jobs/<int:job_index>", methods=["PATCH"])
def api_update_job(job_index):
    if job_index < 0 or job_index >= len(JOBS):
        return jsonify({"error": "Invalid job index"}), 404
    data = request.get_json()
    if "status" in data:
        JOBS[job_index]["status"] = data["status"]
    if "notes" in data:
        JOBS[job_index]["notes"] = data["notes"]
    save_jobs(JOBS, DATA_FILE)
    return jsonify({"ok": True, "job": JOBS[job_index]})


@app.route("/api/reload")
def api_reload():
    global JOBS, DATA_FILE
    JOBS, DATA_FILE = load_jobs()
    return jsonify({"ok": True, "data_file": DATA_FILE, "count": len(JOBS)})


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
        if "interval_hours" in data:
            SCHEDULE_CONFIG["interval_hours"] = int(data["interval_hours"])
        # Recompute next_run
        if SCHEDULE_CONFIG["enabled"]:
            last = SCHEDULE_CONFIG.get("last_run")
            interval = SCHEDULE_CONFIG["interval_hours"]
            if last:
                base = datetime.fromisoformat(last)
            else:
                base = datetime.now()
            SCHEDULE_CONFIG["next_run"] = (base + timedelta(hours=interval)).isoformat()
        else:
            SCHEDULE_CONFIG["next_run"] = None
        save_schedule(SCHEDULE_CONFIG)
        return jsonify({"ok": True, "schedule": SCHEDULE_CONFIG})
    # Refresh from disk
    SCHEDULE_CONFIG = load_schedule()
    return jsonify(SCHEDULE_CONFIG)


def compute_stats():
    tiers, tracks, sources, columns = {}, {}, {}, {}
    for j in JOBS:
        t = j.get("relevance", "Unknown")
        tiers[t] = tiers.get(t, 0) + 1
        tk = j.get("career_track", "Unknown")
        tracks[tk] = tracks.get(tk, 0) + 1
        s = j.get("source", "Unknown")
        sources[s] = sources.get(s, 0) + 1
        c = j.get("status", "New")
        columns[c] = columns.get(c, 0) + 1
    return {
        "total": len(JOBS),
        "by_tier": tiers,
        "by_track": tracks,
        "by_source": sources,
        "by_column": columns,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# HTML / CSS / JS
# ═══════════════════════════════════════════════════════════════════════════════

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔍 Ian's Embedded Job Board</title>
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
.header .meta{font-size:0.85rem;color:#94a3b8;}
.header .meta code{color:#fbbf24;font-size:0.8rem;}
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
.btn.danger{background:#991b1b;border-color:#991b1b;color:#fff;}
.btn.danger:hover{background:#7f1d1d;}
.btn:disabled{opacity:0.4;cursor:not-allowed;}

/* ── Panel (collapsible) ─────────────────────────────────────────────── */
.panel{
  background:#1a2332;border-bottom:1px solid #334155;
  padding:0 28px;overflow:hidden;transition:max-height .3s;
  max-height:0;
}
.panel.open{max-height:500px;padding:16px 28px;}
.panel-title{
  font-size:0.9rem;font-weight:600;margin-bottom:8px;
  display:flex;align-items:center;gap:6px;
}
.panel .console{
  background:#0a0f1a;color:#86efac;border:1px solid #1e3a5f;
  border-radius:8px;padding:12px 16px;font-family:'SF Mono','Fira Code','Cascadia Code',monospace;
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

/* Toggle switch */
.toggle{position:relative;display:inline-block;width:48px;height:26px;}
.toggle input{opacity:0;width:0;height:0;}
.toggle .slider{
  position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;
  background:#334155;border-radius:26px;transition:.2s;
}
.toggle .slider::before{
  content:"";position:absolute;height:20px;width:20px;left:3px;bottom:3px;
  background:#94a3b8;border-radius:50%;transition:.2s;
}
.toggle input:checked+.slider{background:#6366f1;}
.toggle input:checked+.slider::before{transform:translateX(22px);background:#fff;}

/* ── Toolbar ─────────────────────────────────────────────────────────── */
.toolbar{
  display:flex;gap:12px;padding:12px 28px;
  background:#1a2332;border-bottom:1px solid #334155;
  flex-wrap:wrap;align-items:center;
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
  display:flex;gap:6px;padding:12px 28px;
  background:#1a2332;border-bottom:1px solid #334155;flex-wrap:wrap;
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
  display:flex;gap:18px;padding:10px 28px;
  background:#151d2a;border-bottom:1px solid #1e293b;
  flex-wrap:wrap;font-size:0.8rem;color:#94a3b8;
}
.stats-bar strong{color:#e2e8f0;}
.stats-bar .sched-info{color:#fbbf24;margin-left:auto;font-size:0.78rem;}

/* ── Card list ───────────────────────────────────────────────────────── */
.card-list{padding:16px 28px;display:flex;flex-direction:column;gap:14px;}
.card{
  background:#1e293b;border:1px solid #334155;border-radius:12px;
  padding:18px 22px;transition:border-color .15s;
  display:flex;flex-direction:column;gap:10px;
}
.card:hover{border-color:#6366f1;}
.card.highlight{border-left:4px solid var(--tier-color,#6366f1);}
.card .row1{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
.card .tier-badge{
  font-size:0.75rem;font-weight:700;padding:3px 10px;border-radius:5px;
  white-space:nowrap;flex-shrink:0;
}
.card .job-title{font-size:1.1rem;font-weight:600;color:#f1f5f9;}
.card .company-name{font-size:0.9rem;color:#94a3b8;white-space:nowrap;}
.card .row2{display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem;color:#94a3b8;}
.card .row2 span{display:flex;align-items:center;gap:4px;}
.card .row3{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.card .tag{
  font-size:0.72rem;padding:3px 9px;border-radius:4px;white-space:nowrap;font-weight:500;
}
.tag-tier{background:#1e3a5f;color:#93c5fd;}
.tag-score{background:#3b1f1f;color:#fca5a5;}
.tag-purity{background:#1e2a1e;color:#86efac;}
.tag-track-ic{background:#064e3b;color:#6ee7b7;}
.tag-track-lead{background:#4a1d96;color:#c4b5fd;}
.tag-track-hybrid{background:#1e3a5f;color:#93c5fd;}
.tag-clearance-ok{background:#064e3b;color:#6ee7b7;}
.tag-clearance-blocked{background:#4a1d1d;color:#fca5a5;}
.tag-kw{background:#1e293b;color:#cbd5e1;border:1px solid #334155;}
.card .row4{
  display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between;
}
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
.card .current-status{
  font-size:0.75rem;padding:6px 14px;border-radius:6px;font-weight:600;white-space:nowrap;
}

/* ── Empty ───────────────────────────────────────────────────────────── */
.empty-state{text-align:center;padding:60px 20px;color:#475569;}
.empty-state .emoji{font-size:3rem;margin-bottom:12px;}

/* ── Spinner ─────────────────────────────────────────────────────────── */
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{
  display:inline-block;width:16px;height:16px;border:2px solid #475569;
  border-top-color:#818cf8;border-radius:50%;animation:spin .8s linear infinite;
  vertical-align:middle;margin-right:4px;
}

/* ── Responsive ──────────────────────────────────────────────────────── */
@media(max-width:700px){
  .header,.toolbar,.status-tabs,.stats-bar,.card-list,.panel{padding-left:14px;padding-right:14px;}
  .card{padding:14px 16px;}.card .job-title{font-size:1rem;}
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 Ian's Embedded Job Board</h1>
    <div class="meta">
      📁 <code id="data-file">—</code> · <span id="job-count">—</span>
      <span id="sched-badge" style="display:none;margin-left:8px;font-size:0.75rem;color:#fbbf24;">⏰ Auto</span>
    </div>
  </div>
  <div class="btn-row">
    <button class="btn" onclick="reloadData()">🔄 Reload</button>
    <button class="btn primary" id="btn-search" onclick="triggerSearch()">🔍 Re-Search</button>
    <button class="btn" id="btn-schedule" onclick="toggleSchedPanel()">⏰ Schedule</button>
  </div>
</div>

<!-- Search console panel -->
<div class="panel" id="search-panel">
  <div class="panel-title">
    <span id="search-status-icon">🔍</span>
    <span id="search-status-text">Search not running</span>
    <span style="font-size:0.7rem;color:#64748b;" id="search-elapsed"></span>
  </div>
  <div class="console" id="search-console">Click 🔍 Re-Search to start a new job scan.</div>
</div>

<!-- Schedule config panel -->
<div class="panel" id="sched-panel">
  <div class="panel-title">⏰ Auto-Search Schedule</div>
  <div class="schedule-row">
    <label class="toggle">
      <input type="checkbox" id="sched-enabled" onchange="updateSchedule()">
      <span class="slider"></span>
    </label>
    <label for="sched-enabled" style="cursor:pointer;">Enable scheduled auto-search</label>
    <label style="margin-left:12px;">Every</label>
    <select id="sched-interval" onchange="updateSchedule()">
      <option value="3">3 hours</option>
      <option value="6" selected>6 hours</option>
      <option value="12">12 hours</option>
      <option value="24">24 hours</option>
    </select>
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
let JOBS=[], DATA_FILE='', STATUSES=[], TIER_COLORS={}, TIER_ORDER=[];
let activeStatus='All';
let searchPollTimer=null;

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
async function loadData(){
  const r=await fetch('/api/jobs'), d=await r.json();
  JOBS=d.jobs; DATA_FILE=d.data_file; STATUSES=d.statuses;
  TIER_COLORS=d.tier_colors; TIER_ORDER=d.tier_order;
  document.getElementById('data-file').textContent=DATA_FILE;
  document.getElementById('job-count').textContent=JOBS.length+' jobs';
  populateFilters();
  renderAll();
  loadSchedule();
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
function renderAll(){
  renderStatusTabs();
  renderStats();
  renderCards();
}

function renderStatusTabs(){
  const bar=document.getElementById('status-tabs');
  let counts={};
  JOBS.forEach(j=>{const s=j.status||'New'; counts[s]=(counts[s]||0)+1;});
  const total=JOBS.length;
  let html=`<div class="status-tab${activeStatus==='All'?' active':''}" onclick="setStatusFilter('All')">📋 All<span class="badge">${total}</span></div>`;
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

async function reloadData(){
  try{await fetch('/api/reload');await loadData();}catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Search trigger & polling
// ═══════════════════════════════════════════════════════════════════════════
async function triggerSearch(){
  const btn=document.getElementById('btn-search');
  const panel=document.getElementById('search-panel');
  const consoleEl=document.getElementById('search-console');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Starting...';
  panel.classList.add('open');
  consoleEl.textContent='Starting search...\n';
  document.getElementById('search-status-text').textContent='Starting...';
  try{
    const r=await fetch('/api/search',{method:'POST'});
    const d=await r.json();
    if(!d.ok){consoleEl.textContent+='⚠️ '+d.message+'\n';btn.disabled=false;btn.innerHTML='🔍 Re-Search';return;}
    consoleEl.textContent+='✅ '+d.message+'\n';
    startPolling();
  }catch(e){
    consoleEl.textContent+='❌ Error: '+e+'\n';
    btn.disabled=false;btn.innerHTML='🔍 Re-Search';
  }
}

function startPolling(){
  if(searchPollTimer) clearInterval(searchPollTimer);
  searchPollTimer=setInterval(pollSearchStatus,1500);
  pollSearchStatus();
}

async function pollSearchStatus(){
  try{
    const r=await fetch('/api/search/status'), s=await r.json();
    const panel=document.getElementById('search-panel');
    const consoleEl=document.getElementById('search-console');
    const statusIcon=document.getElementById('search-status-icon');
    const statusText=document.getElementById('search-status-text');
    const elapsed=document.getElementById('search-elapsed');
    const btn=document.getElementById('btn-search');

    // Append new lines
    if(s.output&&s.output.length>0){
      const currentLines=consoleEl.textContent.split('\n').filter(Boolean);
      const newLines=s.output.filter(line=>!currentLines.includes(line[1]));
      if(newLines.length>0){
        consoleEl.textContent+=newLines.map(l=>`[${l[0]}] ${l[1]}`).join('\n')+'\n';
        consoleEl.scrollTop=consoleEl.scrollHeight;
      }
    }

    if(s.running){
      statusIcon.textContent='⏳';statusText.textContent='Search running...';
      if(s.start_time){
        const secs=Math.floor((Date.now()-new Date(s.start_time).getTime())/1000);
        elapsed.textContent=`(${Math.floor(secs/60)}m ${secs%60}s)`;
      }
      btn.disabled=true;btn.innerHTML='<span class="spinner"></span> Running...';
      panel.classList.add('open');
    }else{
      if(searchPollTimer){clearInterval(searchPollTimer);searchPollTimer=null;}
      if(s.exit_code===0){
        statusIcon.textContent='✅';statusText.textContent='Search completed successfully!';
        consoleEl.textContent+='\n✅ Search finished. Reloading data...\n';
        await reloadData();
        consoleEl.textContent+='✅ Data reloaded!\n';
      }else if(s.exit_code!==null){
        statusIcon.textContent='❌';statusText.textContent='Search failed (exit '+s.exit_code+')';
      }
      elapsed.textContent='';
      btn.disabled=false;btn.innerHTML='🔍 Re-Search';
    }
  }catch(e){console.error(e);}
}

// ═══════════════════════════════════════════════════════════════════════════
// Schedule
// ═══════════════════════════════════════════════════════════════════════════
function toggleSchedPanel(){
  document.getElementById('sched-panel').classList.toggle('open');
}

async function loadSchedule(){
  try{
    const r=await fetch('/api/schedule'), s=await r.json();
    document.getElementById('sched-enabled').checked=s.enabled;
    document.getElementById('sched-interval').value=s.interval_hours;
    updateSchedDisplay(s);
  }catch(e){console.error(e);}
}

async function updateSchedule(){
  const enabled=document.getElementById('sched-enabled').checked;
  const intervalHours=parseInt(document.getElementById('sched-interval').value);
  try{
    const r=await fetch('/api/schedule',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled,interval_hours}),
    });
    const s=await r.json();
    updateSchedDisplay(s.schedule||s);
  }catch(e){console.error(e);}
}

function updateSchedDisplay(s){
  const badge=document.getElementById('sched-badge');
  const info=document.getElementById('sched-info');
  const nextRun=document.getElementById('sched-next-run');
  if(s.enabled){
    badge.style.display='inline';
    let txt=`Auto: every ${s.interval_hours}h`;
    if(s.next_run){
      const d=new Date(s.next_run);
      txt+=` · Next: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;
    }
    if(s.last_run){
      const d=new Date(s.last_run);
      txt+=` · Last: ${d.toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;
    }
    info.textContent=txt;
  }else{
    badge.style.display='none';
    info.textContent='';
  }
  nextRun.textContent=s.next_run&&s.enabled
    ? `Next run: ${new Date(s.next_run).toLocaleString('en-AU',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}`
    : '';
}

// Keyboard
document.addEventListener('keydown',e=>{if(e.key==='r'&&e.ctrlKey){e.preventDefault();reloadData();}});

// Boot
loadData();
</script>
</body>
</html>"""

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("🔍 Ian's Embedded Job Board")
    print(f"   Data : {DATA_FILE or '(no kanban JSON — run linkedin_job_search.py first)'}")
    print(f"   URL  : http://192.168.44.128:{PORT}")
    print(f"   Schedule: {'ON' if SCHEDULE_CONFIG.get('enabled') else 'OFF'} "
          f"(every {SCHEDULE_CONFIG.get('interval_hours', 6)}h)")
    print(f"   Press Ctrl+C to stop")
    print("=" * 60)
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False)
    finally:
        SCHEDULE_STOP.set()
