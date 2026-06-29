#!/usr/bin/env python3
"""
Kanban Job Board — Chrome web UI for Ian's embedded job search results.

Usage:
    source .venv/bin/activate
    pip install flask
    python job_board.py

    Then open Chrome → http://localhost:3000

The board auto-loads the latest kanban_jobs_*.json and lets you:
  - View jobs in a Kanban board (New → Applied → Interview → Offer → Rejected)
  - Move cards between columns (status persists to JSON)
  - Filter by relevance tier, career track, source, embedded depth
  - Search by keyword
  - See stats summary
"""

import json
import glob
import os
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

# ── Config ───────────────────────────────────────────────────────────────────
PORT = 5000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

app = Flask(__name__)

# ── Kanban column definitions ────────────────────────────────────────────────
KANBAN_COLUMNS = [
    {"id": "New",         "emoji": "📥", "label": "New",          "color": "#6366f1"},
    {"id": "Applied",     "emoji": "📝", "label": "Applied",      "color": "#f59e0b"},
    {"id": "Interview",   "emoji": "📞", "label": "Interview",    "color": "#8b5cf6"},
    {"id": "Offer",       "emoji": "🎉", "label": "Offer",        "color": "#10b981"},
    {"id": "Rejected",    "emoji": "❌", "label": "Rejected",     "color": "#6b7280"},
]

# Relevance tier colors
TIER_COLORS = {
    "🔥🔥 Bare-Metal Gold":  "#fbbf24",
    "🔥 Strong Match":       "#f97316",
    "✅ Good Match":         "#22c55e",
    "⚠️ Possible Match":     "#eab308",
    "🤔 Weak Signal":        "#9ca3af",
    "❌ IT Noise / Irrelevant": "#6b7280",
}

TIER_ORDER = [
    "🔥🔥 Bare-Metal Gold",
    "🔥 Strong Match",
    "✅ Good Match",
    "⚠️ Possible Match",
    "🤔 Weak Signal",
    "❌ IT Noise / Irrelevant",
]

# ── Data loading ─────────────────────────────────────────────────────────────

def find_latest_kanban():
    """Find the most recent kanban JSON file."""
    files = glob.glob(os.path.join(SCRIPT_DIR, "kanban_jobs_*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def load_jobs():
    """Load jobs from the latest kanban JSON."""
    path = find_latest_kanban()
    if not path:
        return [], None
    with open(path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    # Ensure every job has the fields we need
    for j in jobs:
        j.setdefault("status", "New")
        j.setdefault("notes", "")
    return jobs, os.path.basename(path)


def save_jobs(jobs, filename):
    """Persist jobs back to JSON."""
    path = os.path.join(SCRIPT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


# In-memory state (loaded at startup)
JOBS, DATA_FILE = load_jobs()

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main Kanban board."""
    return _HTML


@app.route("/api/jobs")
def api_jobs():
    """Return all jobs as JSON."""
    return jsonify({
        "data_file": DATA_FILE,
        "columns": KANBAN_COLUMNS,
        "tier_order": TIER_ORDER,
        "tier_colors": TIER_COLORS,
        "jobs": JOBS,
        "stats": compute_stats(),
    })


@app.route("/api/jobs/<int:job_index>", methods=["PATCH"])
def api_update_job(job_index):
    """Update a job's status or notes."""
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
    """Reload jobs from the latest kanban JSON file."""
    global JOBS, DATA_FILE
    JOBS, DATA_FILE = load_jobs()
    return jsonify({"ok": True, "data_file": DATA_FILE, "count": len(JOBS)})


def compute_stats():
    """Compute summary stats for the dashboard."""
    tiers = {}
    tracks = {}
    sources = {}
    columns = {}
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
# HTML / CSS / JS (single-page app)
# ═══════════════════════════════════════════════════════════════════════════════

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔍 Ian's Embedded Job Board</title>
<style>
/* ── Reset & Base ─────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: #0f172a; color: #e2e8f0; min-height: 100vh;
}

/* ── Header ───────────────────────────────────────────────────────────── */
.header {
  background: #1e293b; border-bottom: 2px solid #334155;
  padding: 12px 24px; display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
}
.header h1 { font-size: 1.25rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.header .meta { font-size: 0.8rem; color: #94a3b8; }
.header .meta code { color: #fbbf24; font-size: 0.75rem; }
.header .actions { display: flex; gap: 8px; }
.header button {
  background: #334155; color: #e2e8f0; border: 1px solid #475569;
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.8rem;
  transition: background 0.15s;
}
.header button:hover { background: #475569; }

/* ── Stats Bar ─────────────────────────────────────────────────────────── */
.stats-bar {
  display: flex; gap: 16px; padding: 10px 24px;
  background: #1a2332; border-bottom: 1px solid #334155;
  overflow-x: auto; flex-wrap: wrap; font-size: 0.8rem;
}
.stat { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.stat .dot { width: 10px; height: 10px; border-radius: 50%; }
.stat strong { color: #f1f5f9; }

/* ── Filters ───────────────────────────────────────────────────────────── */
.filters {
  display: flex; gap: 10px; padding: 10px 24px;
  background: #1a2332; border-bottom: 1px solid #334155;
  flex-wrap: wrap; align-items: center;
}
.filters select, .filters input {
  background: #0f172a; color: #e2e8f0; border: 1px solid #475569;
  padding: 6px 10px; border-radius: 6px; font-size: 0.8rem;
}
.filters input { flex: 1; min-width: 180px; max-width: 300px; }
.filters select { cursor: pointer; }
.filters label { font-size: 0.75rem; color: #94a3b8; }

/* ── Kanban Board ──────────────────────────────────────────────────────── */
.board {
  display: flex; gap: 12px; padding: 16px 24px;
  overflow-x: auto; min-height: calc(100vh - 170px);
}
.column {
  flex: 1; min-width: 280px; max-width: 380px;
  background: #1e293b; border-radius: 10px;
  display: flex; flex-direction: column;
}
.column-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 2px solid var(--col);
  font-weight: 600; font-size: 0.85rem;
  position: sticky; top: 0; background: #1e293b; border-radius: 10px 10px 0 0;
}
.column-count {
  background: #334155; color: #94a3b8; font-size: 0.7rem;
  padding: 2px 8px; border-radius: 10px; font-weight: 500;
}
.column-body {
  flex: 1; padding: 8px; overflow-y: auto; min-height: 200px;
  display: flex; flex-direction: column; gap: 8px;
}

/* ── Job Card ──────────────────────────────────────────────────────────── */
.card {
  background: #0f172a; border: 1px solid #334155; border-radius: 8px;
  padding: 12px; cursor: pointer; transition: all 0.15s;
  position: relative;
}
.card:hover { border-color: #6366f1; box-shadow: 0 0 0 1px #6366f140; }
.card .relevance-badge {
  display: inline-block; font-size: 0.65rem; font-weight: 700;
  padding: 2px 7px; border-radius: 4px; margin-bottom: 6px;
}
.card .title {
  font-size: 0.85rem; font-weight: 600; color: #f1f5f9;
  margin-bottom: 4px; line-height: 1.3;
}
.card .company {
  font-size: 0.78rem; color: #94a3b8; margin-bottom: 2px;
}
.card .meta-row {
  display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px;
  font-size: 0.68rem; color: #64748b;
}
.card .meta-row span {
  background: #1e293b; padding: 1px 6px; border-radius: 4px;
  white-space: nowrap;
}
.card .tags {
  display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px;
}
.card .tag {
  font-size: 0.6rem; padding: 1px 5px; border-radius: 3px;
  background: #1e3a5f; color: #93c5fd; white-space: nowrap;
}
.card .tag.track-ic { background: #064e3b; color: #6ee7b7; }
.card .tag.track-lead { background: #4a1d96; color: #c4b5fd; }
.card .tag.track-hybrid { background: #1e3a5f; color: #93c5fd; }
.card .tag.source-linkedin { background: #1e3a5f; color: #93c5fd; }
.card .tag.source-indeed { background: #3b1f1f; color: #fca5a5; }
.card .tag.source-google { background: #1e2a1e; color: #86efac; }
.card .tag.clearance-blocked { background: #4a1d1d; color: #fca5a5; }
.card .tag.clearance-ok { background: #064e3b; color: #6ee7b7; }

.card .move-btns {
  display: flex; gap: 4px; margin-top: 8px; flex-wrap: wrap;
}
.card .move-btns button {
  font-size: 0.6rem; padding: 3px 8px; border-radius: 4px;
  border: 1px solid #475569; background: #1e293b; color: #94a3b8;
  cursor: pointer; transition: all 0.15s;
}
.card .move-btns button:hover { background: #334155; color: #e2e8f0; }
.card .move-btns button.active { background: #6366f1; border-color: #6366f1; color: #fff; }

.card .url-link {
  font-size: 0.65rem; color: #6366f1; text-decoration: none;
  display: inline-block; margin-top: 6px;
}
.card .url-link:hover { text-decoration: underline; }

/* ── Empty state ───────────────────────────────────────────────────────── */
.empty-state {
  text-align: center; padding: 40px 20px; color: #475569;
}
.empty-state .emoji { font-size: 3rem; margin-bottom: 8px; }

/* ── Responsive ────────────────────────────────────────────────────────── */
@media (max-width: 900px) {
  .board { flex-direction: column; }
  .column { max-width: 100%; }
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 Ian's Embedded Job Board</h1>
    <div class="meta">Data: <code id="data-file">—</code> · <span id="job-count">—</span></div>
  </div>
  <div class="actions">
    <button onclick="reloadData()" title="Reload from latest JSON">🔄 Reload</button>
  </div>
</div>

<div class="stats-bar" id="stats-bar"></div>

<div class="filters">
  <label>Filter:</label>
  <select id="filter-tier" onchange="renderBoard()">
    <option value="">All Tiers</option>
  </select>
  <select id="filter-track" onchange="renderBoard()">
    <option value="">All Tracks</option>
  </select>
  <select id="filter-source" onchange="renderBoard()">
    <option value="">All Sources</option>
  </select>
  <input type="text" id="filter-search" placeholder="🔍 Search title, company, keywords..."
         oninput="renderBoard()">
  <span style="font-size:0.7rem;color:#64748b;" id="filter-count"></span>
</div>

<div class="board" id="board"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let JOBS = [];
let DATA_FILE = '';
let COLUMNS = [];
let TIER_COLORS = {};
let TIER_ORDER = [];

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
async function loadData() {
  const res = await fetch('/api/jobs');
  const data = await res.json();
  JOBS = data.jobs;
  DATA_FILE = data.data_file;
  COLUMNS = data.columns;
  TIER_COLORS = data.tier_colors;
  TIER_ORDER = data.tier_order;

  document.getElementById('data-file').textContent = DATA_FILE;
  document.getElementById('job-count').textContent = `${JOBS.length} jobs`;
  populateFilters();
  renderStats(data.stats);
  renderBoard();
}

function populateFilters() {
  // Build tier filter options
  const tierSel = document.getElementById('filter-tier');
  tierSel.innerHTML = '<option value="">All Tiers</option>';
  const tiers = [...new Set(JOBS.map(j => j.relevance))];
  tiers.sort((a,b) => TIER_ORDER.indexOf(a) - TIER_ORDER.indexOf(b));
  tiers.forEach(t => { const o = document.createElement('option'); o.value=t; o.textContent=t; tierSel.appendChild(o); });

  // Track filter
  const trackSel = document.getElementById('filter-track');
  trackSel.innerHTML = '<option value="">All Tracks</option>';
  const tracks = [...new Set(JOBS.map(j => j.career_track))].sort();
  tracks.forEach(t => { const o = document.createElement('option'); o.value=t; o.textContent=t; trackSel.appendChild(o); });

  // Source filter
  const srcSel = document.getElementById('filter-source');
  srcSel.innerHTML = '<option value="">All Sources</option>';
  const sources = [...new Set(JOBS.map(j => j.source))].sort();
  sources.forEach(s => { const o = document.createElement('option'); o.value=s; o.textContent=s; srcSel.appendChild(o); });
}

// ═══════════════════════════════════════════════════════════════════════════
// Stats
// ═══════════════════════════════════════════════════════════════════════════
function renderStats(stats) {
  const bar = document.getElementById('stats-bar');
  let html = `<div class="stat"><strong>Total:</strong> ${stats.total}</div>`;
  for (const [col, count] of Object.entries(stats.by_column || {})) {
    html += `<div class="stat"><strong>${col}:</strong> ${count}</div>`;
  }
  bar.innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════════════════
// Board Rendering
// ═══════════════════════════════════════════════════════════════════════════
function getFilteredJobs() {
  const tier = document.getElementById('filter-tier').value;
  const track = document.getElementById('filter-track').value;
  const source = document.getElementById('filter-source').value;
  const search = document.getElementById('filter-search').value.toLowerCase();

  return JOBS.filter(j => {
    if (tier && j.relevance !== tier) return false;
    if (track && j.career_track !== track) return false;
    if (source && j.source !== source) return false;
    if (search) {
      const haystack = `${j.title} ${j.company} ${j.matched_on} ${j.track_detail} ${j.notes}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

function renderBoard() {
  const board = document.getElementById('board');
  const filtered = getFilteredJobs();
  document.getElementById('filter-count').textContent = `Showing ${filtered.length} of ${JOBS.length}`;

  let html = '';
  for (const col of COLUMNS) {
    const colJobs = filtered.filter(j => (j.status || 'New') === col.id);
    html += `<div class="column" style="--col:${col.color}">`;
    html += `<div class="column-header" style="border-color:${col.color}">`;
    html += `<span>${col.emoji} ${col.label}</span>`;
    html += `<span class="column-count">${colJobs.length}</span>`;
    html += `</div>`;
    html += `<div class="column-body" data-status="${col.id}"`;
    html += ` ondragover="handleDragOver(event)" ondrop="handleDrop(event, '${col.id}')">`;
    if (colJobs.length === 0) {
      html += `<div class="empty-state"><div class="emoji">${col.emoji}</div>No jobs</div>`;
    }
    for (const [idx, job] of colJobs.entries()) {
      const globalIdx = JOBS.indexOf(job);
      html += renderCard(job, globalIdx);
    }
    html += `</div></div>`;
  }
  board.innerHTML = html;
}

function renderCard(job, idx) {
  const tierColor = TIER_COLORS[job.relevance] || '#6b7280';
  const purityPct = job.purity != null ? Math.round(job.purity * 100) + '%' : '';

  // Build tags
  let tagsHtml = '';
  if (job.embedded_tier) {
    tagsHtml += `<span class="tag">🏷 ${job.embedded_tier}</span>`;
  }
  if (job.score) {
    tagsHtml += `<span class="tag">⭐ ${job.score}</span>`;
  }
  if (purityPct) {
    tagsHtml += `<span class="tag">🧪 ${purityPct}</span>`;
  }
  // Career track tag
  const trackClass = job.career_track && job.career_track.includes('Lead') ? 'track-lead'
    : job.career_track && job.career_track.includes('Hybrid') ? 'track-hybrid' : 'track-ic';
  if (job.career_track) {
    tagsHtml += `<span class="tag ${trackClass}">${job.career_track}</span>`;
  }
  // Source tag
  const srcClass = 'source-' + (job.source || 'unknown');
  tagsHtml += `<span class="tag ${srcClass}">📎 ${job.source}</span>`;
  // Clearance tag
  if (job.clearance_status) {
    const blocked = job.clearance_status.includes('BLOCKED');
    tagsHtml += `<span class="tag ${blocked ? 'clearance-blocked' : 'clearance-ok'}">${job.clearance_status}</span>`;
  }

  // Matched keywords
  if (job.matched_on) {
    const kws = job.matched_on.split(', ').slice(0, 4);
    tagsHtml += kws.map(k => `<span class="tag">#${k}</span>`).join('');
  }

  // Move buttons (all columns except current)
  let moveBtns = '';
  for (const col of COLUMNS) {
    if (col.id === (job.status || 'New')) continue;
    moveBtns += `<button onclick="moveJob(${idx}, '${col.id}')" title="Move to ${col.label}">${col.emoji} ${col.label}</button>`;
  }

  return `
    <div class="card" draggable="true" data-job-idx="${idx}"
         ondragstart="handleDragStart(event, ${idx})" ondragend="handleDragEnd(event)">
      <div class="relevance-badge" style="background:${tierColor}20;color:${tierColor};border:1px solid ${tierColor}40;">
        ${job.relevance || 'Unknown'}
      </div>
      <div class="title">${escHtml(job.title)}</div>
      <div class="company">🏢 ${escHtml(job.company)}</div>
      <div class="meta-row">
        <span>📍 ${escHtml(job.location)}</span>
        <span>📅 ${job.date_posted || '?'}</span>
      </div>
      <div class="tags">${tagsHtml}</div>
      <a class="url-link" href="${escHtml(job.url)}" target="_blank" rel="noopener"
         onclick="event.stopPropagation()">🔗 View Job →</a>
      <div class="move-btns">${moveBtns}</div>
    </div>`;
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ═══════════════════════════════════════════════════════════════════════════
// Drag & Drop
// ═══════════════════════════════════════════════════════════════════════════
let dragIdx = null;

function handleDragStart(e, idx) {
  dragIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
  e.target.style.opacity = '0.5';
}

function handleDragEnd(e) {
  e.target.style.opacity = '1';
  dragIdx = null;
}

function handleDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
}

function handleDrop(e, status) {
  e.preventDefault();
  if (dragIdx != null) {
    moveJob(dragIdx, status);
    dragIdx = null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Actions
// ═══════════════════════════════════════════════════════════════════════════
async function moveJob(idx, newStatus) {
  const oldStatus = JOBS[idx].status || 'New';
  if (oldStatus === newStatus) return;

  try {
    const res = await fetch(`/api/jobs/${idx}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: newStatus}),
    });
    if (res.ok) {
      JOBS[idx].status = newStatus;
      renderBoard();
    }
  } catch (err) {
    console.error('Failed to update job:', err);
  }
}

async function reloadData() {
  try {
    await fetch('/api/reload');
    await loadData();
  } catch (err) {
    console.error('Reload failed:', err);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Keyboard shortcuts
// ═══════════════════════════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.key === 'r' && e.ctrlKey) { e.preventDefault(); reloadData(); }
});

// Boot
loadData();
</script>
</body>
</html>"""

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("🔍 Ian's Embedded Job Board")
    print(f"   Data: {DATA_FILE or '(no kanban JSON found — run linkedin_job_search.py first)'}")
    print(f"   Open Chrome → http://localhost:{PORT}")
    print(f"   Press Ctrl+C to stop")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False)
