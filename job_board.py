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
"""

import json
import glob
import os
from datetime import datetime

from flask import Flask, jsonify, request

# ── Config ───────────────────────────────────────────────────────────────────
PORT = 5000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

app = Flask(__name__)

# ── Status definitions ───────────────────────────────────────────────────────
STATUSES = [
    {"id": "New",       "emoji": "📥", "label": "New"},
    {"id": "Applied",   "emoji": "📝", "label": "Applied"},
    {"id": "Interview", "emoji": "📞", "label": "Interview"},
    {"id": "Offer",     "emoji": "🎉", "label": "Offer"},
    {"id": "Rejected",  "emoji": "❌", "label": "Rejected"},
]

# Relevance tier colors (HSL-friendly for dark theme)
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
    files = glob.glob(os.path.join(SCRIPT_DIR, "kanban_jobs_*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


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
/* ── Reset ─────────────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               'Helvetica Neue', Arial, sans-serif;
  background:#0f172a; color:#e2e8f0; min-height:100vh;
  font-size:16px; line-height:1.5;
}

/* ── Header ────────────────────────────────────────────────────────────── */
.header{
  background:#1e293b; border-bottom:2px solid #334155;
  padding:14px 28px; display:flex; align-items:center; justify-content:space-between;
  position:sticky; top:0; z-index:100; gap:16px; flex-wrap:wrap;
}
.header h1{font-size:1.3rem; font-weight:700;}
.header .meta{font-size:0.85rem; color:#94a3b8;}
.header .meta code{color:#fbbf24; font-size:0.8rem;}
.header button{
  background:#334155; color:#e2e8f0; border:1px solid #475569;
  padding:7px 18px; border-radius:8px; cursor:pointer; font-size:0.85rem;
  transition:background .15s; white-space:nowrap;
}
.header button:hover{background:#475569;}

/* ── Filters bar ────────────────────────────────────────────────────────── */
.toolbar{
  display:flex; gap:12px; padding:12px 28px;
  background:#1a2332; border-bottom:1px solid #334155;
  flex-wrap:wrap; align-items:center;
}
.toolbar label{font-size:0.8rem; color:#94a3b8; white-space:nowrap;}
.toolbar select, .toolbar input{
  background:#0f172a; color:#e2e8f0; border:1px solid #475569;
  padding:7px 12px; border-radius:8px; font-size:0.85rem;
}
.toolbar input{flex:1; min-width:200px; max-width:340px;}
.toolbar select{cursor:pointer;}
.toolbar .count{font-size:0.8rem; color:#64748b; margin-left:auto;}

/* ── Status tabs ────────────────────────────────────────────────────────── */
.status-tabs{
  display:flex; gap:6px; padding:12px 28px;
  background:#1a2332; border-bottom:1px solid #334155;
  flex-wrap:wrap;
}
.status-tab{
  padding:8px 18px; border-radius:20px; border:1px solid #475569;
  background:#0f172a; color:#94a3b8; cursor:pointer; font-size:0.85rem;
  transition:all .15s; white-space:nowrap; font-weight:500;
}
.status-tab:hover{background:#1e293b; color:#e2e8f0;}
.status-tab.active{background:#6366f1; border-color:#6366f1; color:#fff;}
.status-tab .badge{
  display:inline-block; background:#00000030; color:inherit;
  padding:1px 9px; border-radius:10px; font-size:0.75rem; margin-left:4px;
}

/* ── Stats bar ──────────────────────────────────────────────────────────── */
.stats-bar{
  display:flex; gap:18px; padding:10px 28px;
  background:#151d2a; border-bottom:1px solid #1e293b;
  flex-wrap:wrap; font-size:0.8rem; color:#94a3b8;
}
.stats-bar strong{color:#e2e8f0;}

/* ── Card list ──────────────────────────────────────────────────────────── */
.card-list{
  padding:16px 28px; display:flex; flex-direction:column; gap:14px;
}
.card{
  background:#1e293b; border:1px solid #334155; border-radius:12px;
  padding:18px 22px; transition:border-color .15s;
  display:flex; flex-direction:column; gap:10px;
}
.card:hover{border-color:#6366f1;}
.card.highlight{border-left:4px solid var(--tier-color,#6366f1);}

/* Row 1: tier badge + title + company */
.card .row1{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;}
.card .tier-badge{
  font-size:0.75rem; font-weight:700; padding:3px 10px; border-radius:5px;
  white-space:nowrap; flex-shrink:0;
}
.card .job-title{
  font-size:1.1rem; font-weight:600; color:#f1f5f9;
}
.card .company-name{
  font-size:0.9rem; color:#94a3b8; white-space:nowrap;
}

/* Row 2: meta info */
.card .row2{
  display:flex; gap:16px; flex-wrap:wrap; font-size:0.85rem; color:#94a3b8;
}
.card .row2 span{display:flex; align-items:center; gap:4px;}

/* Row 3: tags */
.card .row3{
  display:flex; gap:6px; flex-wrap:wrap; align-items:center;
}
.card .tag{
  font-size:0.72rem; padding:3px 9px; border-radius:4px; white-space:nowrap;
  font-weight:500;
}
.tag-tier{background:#1e3a5f; color:#93c5fd;}
.tag-score{background:#3b1f1f; color:#fca5a5;}
.tag-purity{background:#1e2a1e; color:#86efac;}
.tag-track-ic{background:#064e3b; color:#6ee7b7;}
.tag-track-lead{background:#4a1d96; color:#c4b5fd;}
.tag-track-hybrid{background:#1e3a5f; color:#93c5fd;}
.tag-source{background:#1e293b; color:#94a3b8; border:1px solid #334155;}
.tag-clearance-ok{background:#064e3b; color:#6ee7b7;}
.tag-clearance-blocked{background:#4a1d1d; color:#fca5a5;}
.tag-kw{background:#1e293b; color:#cbd5e1; border:1px solid #334155;}

/* Row 4: actions */
.card .row4{
  display:flex; gap:8px; flex-wrap:wrap; align-items:center;
  justify-content:space-between;
}
.card .row4 .left{display:flex; gap:8px; align-items:center;}
.card .url-btn{
  display:inline-flex; align-items:center; gap:4px;
  color:#818cf8; text-decoration:none; font-size:0.85rem; font-weight:500;
  padding:6px 12px; border-radius:6px; border:1px solid #818cf840;
  transition:background .15s;
}
.card .url-btn:hover{background:#818cf815;}

.card .status-btns{display:flex; gap:5px; flex-wrap:wrap;}
.card .status-btn{
  font-size:0.75rem; padding:6px 14px; border-radius:6px;
  border:1px solid #475569; background:#0f172a; color:#94a3b8;
  cursor:pointer; transition:all .15s; font-weight:500;
}
.card .status-btn:hover{background:#334155; color:#e2e8f0; border-color:#6366f1;}
.card .status-btn.current{
  background:var(--btn-color,#6366f1); border-color:var(--btn-color,#6366f1);
  color:#fff; cursor:default;
}
.card .current-status{
  font-size:0.75rem; padding:6px 14px; border-radius:6px;
  font-weight:600; white-space:nowrap;
}

/* ── Empty state ────────────────────────────────────────────────────────── */
.empty-state{
  text-align:center; padding:60px 20px; color:#475569;
}
.empty-state .emoji{font-size:3rem; margin-bottom:12px;}

/* ── Responsive ─────────────────────────────────────────────────────────── */
@media(max-width:700px){
  .header,.toolbar,.status-tabs,.stats-bar,.card-list{padding-left:14px;padding-right:14px;}
  .card{padding:14px 16px;}
  .card .job-title{font-size:1rem;}
}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🔍 Ian's Embedded Job Board</h1>
    <div class="meta">📁 <code id="data-file">—</code> &nbsp;·&nbsp; <span id="job-count">—</span></div>
  </div>
  <button onclick="reloadData()">🔄 Reload</button>
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

function setStatusFilter(s){
  activeStatus=s; renderAll();
}

function renderStats(){
  const bar=document.getElementById('stats-bar');
  const filtered=getFiltered();
  let cols={}; filtered.forEach(j=>{const s=j.status||'New'; cols[s]=(cols[s]||0)+1;});
  bar.innerHTML=`Showing <strong>${filtered.length}</strong> of <strong>${JOBS.length}</strong> jobs`+
    STATUSES.map(st=>` &nbsp;·&nbsp; ${st.emoji} <strong>${cols[st.id]||0}</strong> ${st.label}`).join('');
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

    // Status buttons (show all except current)
    let statusBtns='';
    STATUSES.forEach(st=>{
      if(st.id===status){
        statusBtns+=`<span class="current-status" style="background:var(--btn-color,#6366f1);color:#fff;">${st.emoji} ${st.label}</span>`;
      }else{
        statusBtns+=`<button class="status-btn" style="--btn-color:${st.id==='New'?'#6366f1':st.id==='Applied'?'#f59e0b':st.id==='Interview'?'#8b5cf6':st.id==='Offer'?'#10b981':'#6b7280'}" onclick="moveJob(${idx},'${st.id}')">${st.emoji} ${st.label}</button>`;
      }
    });

    // Keyword tags (first 5)
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
        <span>📍 ${escHtml(j.location)}</span>
        <span>📅 ${j.date_posted||'?'}</span>
        <span>📎 ${escHtml(j.source||'?')}</span>
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
// Actions
// ═══════════════════════════════════════════════════════════════════════════
async function moveJob(idx,newStatus){
  try{
    const r=await fetch('/api/jobs/'+idx,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:newStatus})});
    if(r.ok){
      const d=await r.json();
      JOBS[idx].status=d.job.status;
      renderAll();
    }
  }catch(e){console.error(e);}
}

async function reloadData(){
  try{
    await fetch('/api/reload');
    await loadData();
  }catch(e){console.error(e);}
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
    print(f"   Data: {DATA_FILE or '(no kanban JSON — run linkedin_job_search.py first)'}")
    print(f"   Open Chrome → http://192.168.44.128:{PORT}")
    print(f"   Press Ctrl+C to stop")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False)
