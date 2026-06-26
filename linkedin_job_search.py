#!/usr/bin/env python3
"""
Melbourne Embedded Systems Job Scraper — tailored to Ian Chang's resume.
Searches LinkedIn, Indeed, Google for roles matching 25+ years of embedded
systems, networking, FPGA/DSP, and 5G/ORAN expertise.
Output: CSV + relevance-scored Kanban JSON.
"""

import csv
import json
import re
from datetime import datetime
from jobspy import scrape_jobs

# ── Search Configuration ──────────────────────────────────────────────────────
# Expanded from resume: Embedded Linux, BSP, Firmware, FPGA, Networking, 5G/ORAN

SEARCH_TERMS = [
    # ── Core Embedded ──
    "embedded software engineer",
    "embedded linux engineer",
    "firmware engineer",
    "embedded systems engineer",
    "BSP engineer",
    "bootloader U-Boot engineer",
    "Yocto Buildroot engineer",
    # ── Linux / Kernel ──
    "linux kernel engineer",
    "device driver engineer",
    "system software engineer linux",
    # ── FPGA / DSP ──
    "FPGA engineer",
    "DSP engineer embedded",
    "hardware engineer embedded",
    # ── Networking / Telecom ──
    "network software engineer embedded",
    "telecom software engineer",
    "5G engineer software",
    "ORAN engineer",
    # ── Senior / Lead (他的資歷層級) ──
    "senior firmware engineer",
    "staff embedded engineer",
    "embedded software manager",
    "board bring-up engineer",
]

LOCATION = "Melbourne, Victoria, Australia"
RESULTS_PER_TERM = 20
HOURS_OLD = 168  # 7 days

# ── Output ────────────────────────────────────────────────────────────────────
timestamp = datetime.now().strftime("%Y%m%d_%H%M")
OUTPUT_CSV = f"melbourne_embedded_jobs_{timestamp}.csv"

# ── Resume-Based Relevance Keywords ───────────────────────────────────────────
# Each category has a weight; matched keywords accumulate score.

RELEVANCE_RULES = [
    # (weight, keywords) — higher weight = stronger signal
    (10, [
        "embedded linux", "yocto", "buildroot", "u-boot", "u-boot",
        "bare-metal", "bare metal", "board bring-up", "board bring up",
        "secure boot", "nxp", "marvell", "oran", "5g ", " openran",
    ]),
    (8, [
        "firmware", "embedded", "bootloader", "bsp ", "board support",
        "kernel driver", "device driver", "linux kernel",
        "fpga", "dsp", "serdes", "pcie driver",
        "network processor", "packet forwarding", "data plane",
        "hardware security", "tam ", "trust anchor",
    ]),
    (6, [
        "embedded c", "embedded c++", "rtos", "real-time",
        "low level", "low-level", "arm ", "cortex-", "cortex ",
        "microcontroller", "mcu", "soc ", "system-on-chip",
        "i2c", "spi", "uart", "mdio", "router ", "switch ",
        "telecom", "telecommunications", "hardware bring",
        "manufacturing test", "diagnostics",
    ]),
    (4, [
        "c++", "c language", "assembly", "linux ",
        "docker", "git", "ci/cd", "agile",
        "networking", "ethernet", "l2 ", "l3 ", "layer 2", "layer 3",
        "ip networking", "routing", "switching",
        "synchronization", "ptp", "sync-e", "synce",
        "high-speed", "high speed", "throughput", "latency",
        "o-ran", "o-ran", "du ", "ru ", "radio unit",
    ]),
    (2, [
        "hardware", "system software", "systems software",
        "software engineer", "senior software", "staff software",
        "lead engineer", "software manager",
        "integration", "debugging", "performance optimization",
        "defence", "aerospace", "avionics", "industrial",
        "iot", "internet of things",
    ]),
]

# Negative keywords — presence strongly suggests NOT an embedded role
NEGATIVE_KEYWORDS = [
    "frontend", "front-end", "full stack", "fullstack", "react", "angular",
    "node.js", "nodejs", "javascript", "typescript", "css ", "html ",
    "ios developer", "android developer", "mobile app", "swift", "kotlin",
    "data scientist", "machine learning engineer", "ml engineer",
    "salesforce", "sap ", "erp ", "crm ",
    "marketing", "sales engineer", "recruiter", "talent acquisition",
    "customer success", "account manager", "product manager",
    "ux ", "ui ", "designer", "graphic",
    "lending platform", "fintech lending", "mortgage",
    "storage engineer", "backup ", "storage admin",
    "business analyst", "scrum master", "agile coach",
    ".net developer", "c# developer", "power platform",
    "databricks", "snowflake", "data engineer", "etl ",
    "ai engineer", "ai/ml", "ai infrastructure",
    "devops", "sre", "site reliability",
    "cloud engineer", "aws architect", "azure architect",
]


def compute_relevance(title, description="", company=""):
    """Score a job against Ian's resume. Returns (score, matched_keywords)."""
    text = f"{title} {description} {company}".lower()
    score = 0
    matched = []

    for weight, keywords in RELEVANCE_RULES:
        for kw in keywords:
            if kw in text:
                score += weight
                matched.append(kw)

    # Penalize negative keywords
    neg_hits = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    if neg_hits > 0:
        score -= neg_hits * 5  # significant penalty

    return max(score, 0), matched


def get_relevance_tier(score):
    """Human-readable relevance tier."""
    if score >= 30:
        return "🔥 Strong Match"
    elif score >= 18:
        return "✅ Good Match"
    elif score >= 10:
        return "⚠️ Possible Match"
    else:
        return "❌ Weak Match"


# ── Scrape ────────────────────────────────────────────────────────────────────
all_jobs = []

print("=" * 72)
print("🔍 Ian Chang — Embedded Systems Job Search")
print(f"   Location: {LOCATION}")
print(f"   Search terms: {len(SEARCH_TERMS)}")
print(f"   Max age: {HOURS_OLD}h ({HOURS_OLD // 24} days)")
print("=" * 72)

for term in SEARCH_TERMS:
    print(f"\n🔎 Searching: '{term}' ...")
    try:
        jobs = scrape_jobs(
            site_name=["linkedin", "indeed", "google"],
            search_term=term,
            google_search_term=f"{term} jobs Melbourne Victoria",
            location=LOCATION,
            results_wanted=RESULTS_PER_TERM,
            hours_old=HOURS_OLD,
            country_indeed="Australia",
            linkedin_fetch_description=True,
        )
        print(f"   → Found {len(jobs)} raw results")
        all_jobs.append(jobs)
    except Exception as e:
        print(f"   ⚠️  Error: {e}")

# ── Combine & Deduplicate ─────────────────────────────────────────────────────
import pandas as pd

if not all_jobs:
    print("\n❌ No results found.")
    exit(1)

combined = pd.concat(all_jobs, ignore_index=True)
before = len(combined)
combined = combined.drop_duplicates(subset=["job_url"], keep="first")
print(f"\n📊 Combined: {before} → {len(combined)} after URL dedup")

# ── Melbourne Location Filter ──────────────────────────────────────────────────
if "location" in combined.columns:
    combined["_loc"] = combined["location"].astype(str).str.lower()
    melb_pattern = (
        "melbourne|victoria|vic|doncaster|templestowe|box hill|"
        "blackburn|nunawading|ringwood|glen waverley|mount waverley|"
        "burwood|camberwell|hawthorn|kew|balwyn|preston|"
        "geelong|ballarat|bendigo|dandenong|frankston|"
        "mornington|werribee|sunbury|craigieburn|epping|"
        "south yarra|richmond|brunswick|northcote|footscray|"
        "st kilda|port melbourne|south melbourne"
    )
    combined = combined[
        combined["_loc"].str.contains(melb_pattern, na=False)
    ]
    print(f"📍 After Melbourne-area filter: {len(combined)} jobs")
    combined = combined.drop(columns=["_loc"])

# ── Relevance Scoring ─────────────────────────────────────────────────────────
print(f"\n🎯 Computing relevance scores based on Ian's resume...")

scores = []
matched_kws = []
tiers = []
notes_list = []

for _, row in combined.iterrows():
    title = str(row.get("title", ""))
    company = str(row.get("company", ""))
    desc = str(row.get("description", ""))
    score, matched = compute_relevance(title, desc, company)
    scores.append(score)
    matched_kws.append(", ".join(matched[:8]))  # top 8 matched keywords
    tiers.append(get_relevance_tier(score))
    notes_list.append(f"Score={score} | Matched: {', '.join(matched[:6])}")

combined["relevance_score"] = scores
combined["relevance_tier"] = tiers
combined["matched_keywords"] = matched_kws
combined["match_notes"] = notes_list

# ── Filter: only keep jobs with decent relevance ──────────────────────────────
MIN_SCORE = 8  # Below this = almost certainly not relevant
filtered = combined[combined["relevance_score"] >= MIN_SCORE].copy()
dropped_count = len(combined) - len(filtered)
if dropped_count > 0:
    lowest = combined[combined["relevance_score"] < MIN_SCORE]
    print(f"🗑️  Dropped {dropped_count} low-relevance jobs (score < {MIN_SCORE}):")
    for _, row in lowest.iterrows():
        t = str(row.get('title', ''))[:60]
        c = str(row.get('company', ''))[:30]
        s = int(row.get('relevance_score', 0))
        print(f"    [{s:2d}] {t} @ {c}")
print(f"✅ Kept {len(filtered)} relevant jobs")

# ── Sort: relevance score first, then date ────────────────────────────────────
if "date_posted" in filtered.columns:
    filtered = filtered.sort_values(
        ["relevance_score", "date_posted"],
        ascending=[False, False]
    )

# ── Save CSV ──────────────────────────────────────────────────────────────────
filtered.to_csv(OUTPUT_CSV, index=False, quoting=csv.QUOTE_NONNUMERIC)
print(f"\n💾 CSV saved to: {OUTPUT_CSV}")

# ── Print Terminal Summary ────────────────────────────────────────────────────
display_cols = [
    "relevance_tier", "title", "company", "location",
    "date_posted", "relevance_score", "matched_keywords"
]
display_cols = [c for c in display_cols if c in filtered.columns]
print(f"\n{'=' * 72}")
print(f"🏆 Top Results (sorted by relevance)")
print(f"{'=' * 72}")
print(filtered[display_cols].head(30).to_string(index=False))

# ── Summary stats ─────────────────────────────────────────────────────────────
print(f"\n{'=' * 72}")
print(f"📈 Summary by relevance tier:")
tier_counts = filtered["relevance_tier"].value_counts()
for tier in ["🔥 Strong Match", "✅ Good Match", "⚠️ Possible Match", "❌ Weak Match"]:
    count = tier_counts.get(tier, 0)
    if count > 0:
        print(f"   {tier}: {count} jobs")

# ── Kanban JSON ───────────────────────────────────────────────────────────────
kanban_file = f"kanban_jobs_{timestamp}.json"
kanban_entries = []

for _, row in filtered.head(40).iterrows():  # top 40 by relevance
    entry = {
        "title": str(row.get("title", "")),
        "company": str(row.get("company", "")),
        "location": str(row.get("location", "")),
        "date_posted": str(row.get("date_posted", "")),
        "url": str(row.get("job_url", "")),
        "source": str(row.get("site", "")),
        "relevance": str(row.get("relevance_tier", "")),
        "score": int(row.get("relevance_score", 0)),
        "matched_on": str(row.get("matched_keywords", "")),
        "status": "New",
        "notes": str(row.get("match_notes", "")),
    }
    kanban_entries.append(entry)

with open(kanban_file, "w", encoding="utf-8") as f:
    json.dump(kanban_entries, f, indent=2, ensure_ascii=False)
print(f"\n📋 Kanban JSON saved to: {kanban_file} ({len(kanban_entries)} entries)")

print(f"\n✅ Done! Run again with: python linkedin_job_search.py")
