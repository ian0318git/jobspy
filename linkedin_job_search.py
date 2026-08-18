#!/usr/bin/env python3
"""
Melbourne Embedded Systems Job Scraper.

Three intelligent engines:
  1. Semantic Depth Brain — distinguishes hardcore embedded (bare-metal, kernel,
     SoC, chip-level) from generic IT noise (cloud, web, AI/ML, DevOps).
  2. IC / Lead Dual Track — classifies every job as Individual Contributor,
     Leadership, or Hybrid; embraces both tracks for maximum opportunity.
  3. Security Clearance Red Line — auto-detects and blocks roles requiring
     NV1/NV2/Baseline clearance or Australian citizenship (may not apply
     to all visa categories), preventing wasted applications.

Output: CSV + relevance-scored Kanban JSON with track & clearance metadata.
"""

import csv
import json
import os
import re
from datetime import datetime
from jobspy import scrape_jobs

# ═══════════════════════════════════════════════════════════════════════════════
# Search Configuration
# ═══════════════════════════════════════════════════════════════════════════════

SEARCH_TERMS = [
    # ── Core Embedded ──
    "embedded software engineer",
    "embedded linux engineer",
    "firmware engineer",
    "embedded systems engineer",
    "BSP engineer",
    "board support package engineer",
    "Yocto Buildroot engineer",
    "bootloader engineer",
    # ── Linux Kernel / Driver ──
    "linux kernel engineer",
    "device driver engineer",
    "kernel module engineer",
    # ── Bare-Metal / RTOS ──
    "bare-metal firmware engineer",
    "RTOS engineer",
    "Zephyr RTOS engineer",
    "FreeRTOS engineer",
    # ── FPGA / DSP / ASIC ──
    "FPGA engineer",
    "DSP engineer embedded",
    "ASIC engineer firmware",
    "HDL Verilog engineer",
    # ── Hardware / Board-Level ──
    "hardware engineer embedded",
    "board bring-up engineer",
    "hardware bring-up engineer",
    "diagnostics engineer embedded",
    # ── Networking / 5G / Telecom ──
    "network software engineer embedded",
    "telecom software engineer",
    "5G engineer software",
    "ORAN engineer",
    # ── Security / Trusted Compute ──
    "secure boot engineer",
    "hardware security engineer",
    "TrustZone engineer",
    # ── Senior IC Track (Individual Contributor) ──
    "principal embedded engineer",
    "staff embedded engineer",
    "senior firmware engineer",
    "senior embedded linux",
    "distinguished engineer embedded",
    # ── Lead Track (Management) ──
    "embedded software manager",
    "firmware engineering manager",
    "embedded team lead",
    "director embedded software",
    # ── Broad catch (may bring noise, filtered later) ──
    "C++ engineer low latency",
    "system software engineer C",
    # ── Robotics / Autonomous Systems ──
    "robotics engineer",
    "robotics software engineer",
    "autonomous systems engineer",
    "autonomous vehicle engineer",
    # ── IoT / Edge Computing ──
    "IoT engineer",
    "internet of things engineer",
    "edge computing engineer",
]

LOCATION = "Melbourne, Victoria, Australia"
RESULTS_PER_TERM = 20
HOURS_OLD = 168  # 7 days

# ═══════════════════════════════════════════════════════════════════════════════
# Output
# ═══════════════════════════════════════════════════════════════════════════════
OUTPUT_DIR = "search_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M")
OUTPUT_CSV = os.path.join(OUTPUT_DIR, f"melbourne_embedded_jobs_{timestamp}.csv")

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 1 — Semantic Depth Brain
# ──────────────────────────────────────────────────────────────────────────────
# Instead of flat keyword counting, we use a layered signal system:
#
#   Layer A — SILICON-LEVEL (weight 15): the hardest embedded signals.
#            Bare-metal, chip-level registers, boot ROM, JTAG, SerDes PHY.
#   Layer B — KERNEL-LEVEL (weight 10): Linux kernel, device drivers, RTOS,
#            BSP, device tree, interrupt handlers, DMA.
#   Layer C — HARDWARE-ADJACENT (weight 6): SoC architecture, FPGA/DSP,
#            hardware bring-up, manufacturing diagnostics, secure boot.
#   Layer D — EMBEDDED-SPECTRUM (weight 3): Firmware, I2C/SPI/UART, ARM
#            Cortex, cross-compilation, Yocto/Buildroot.
#
#   IT_NOISE (weight -8 each): cloud, web frameworks, pure database, AI/ML
#            pipeline, DevOps/SRE, frontend JS — the "pure software" stack.
#
#   CONTEXT BOOST: some terms only score when they appear alongside
#            hardware-layer signals (e.g. "linux" alone = nothing;
#            "linux" + "kernel driver" = kernel-level signal).
# ═══════════════════════════════════════════════════════════════════════════════

SEMANTIC_DEPTH_LAYERS = [

    # ── Layer A: SILICON-LEVEL (weight 15) ──
    (15, "silicon", [
        "bare-metal", "bare metal", "baremetal",
        "board bring-up", "board bring up", "board bringup",
        "boot rom", "bootrom", "first stage boot", "second stage boot",
        "jtag", "swd ", "lauterbach", "trace32", "trace 32",
        "logic analyzer", "signal integrity", "oscilloscope diagnose",
        "serdes", "25g serdes", "10g serdes", "pcie gen", "pci express",
        "ddr4", "ddr5", "lpddr", "ddr timing", "dram controller",
        "memory controller", "memory-mapped", "mmio", "register-level",
        "chip-level debug", "silicon bring", "post-silicon",
        "pre-silicon", "asic bring", "soc bring",
        "secure boot", "chain of trust", "root of trust",
        "trusted execution environment", "tee ", "trustzone",
        "hardware root of trust", "measured boot",
        "nxp lx2160", "nxp layerscape", "marvell cnf", "marvell octeon",
        "marvell armada", "intel rangeley", "intel skylake",
        "cortex-a72", "cortex-a53", "cortex-a9", "cortex-m4", "cortex-m7",
        "cortex-r5", "cortex-r", "armv8", "armv9",
    ]),

    # ── Layer B: KERNEL-LEVEL (weight 10) ──
    (10, "kernel", [
        "linux kernel", "kernel module", "kernel driver", "kernel space",
        "kernel development", "kernel programming",
        "device driver", "ethernet driver", "network driver",
        "pcie driver", "dma driver", "i2c driver", "spi driver",
        "device tree", "dts file", "dts ", "dtb ",
        "u-boot", "u-boot", "das u-boot", "bootloader",
        "interrupt handler", "isr ", "interrupt service routine",
        "rtos", "freertos", "zephyr", "threadx", "vxworks", "qnx",
        "dma ", "direct memory access",
        "kconfig", "kbuild", "kernel config",
        "rootfs", "initramfs", "busybox", "buildroot",
        "cross-compile", "cross-compilation", "toolchain",
        "yocto", "openembedded", "bitbake", "yocto project",
        "bsp ", "board support package",
        "gdb ", "kgdb", "jtag debug",
    ]),

    # ── Layer C: HARDWARE-ADJACENT (weight 6) ──
    (6, "hardware", [
        "fpga", "cpld", "hdl ", "verilog", "vhdl",
        "dsp ", "digital signal process",
        "soc architecture", "system-on-chip", "system on chip",
        "microcontroller", "mcu ", "soc ",
        "hardware bring", "hardware debug", "hardware design",
        "hardware-software", "electronics",
        "manufacturing test", "factory test", "production test",
        "diagnostics firmware", "power-on self-test", "power on self test",
        "secure enclave", "hsm ", "hardware security module",
        "tam ", "trust anchor", "sudi", "cisco tam",
        "hardware validation", "board validation",
        "phy ", "mdio", "sgmii", "rgmii", "xfi ",
        "i2c", "spi", "uart", "gpio", "pwm", "adc", "dac",
        "sensor", "actuator", "can bus", "modbus",
        "ethernet", "tcp/ip offload", "packet processing",
        "network processor", "data plane", "forwarding engine",
    ]),

    # ── Layer D: EMBEDDED-SPECTRUM (weight 3) ──
    (3, "embedded", [
        "firmware", "f/w", "embedded software", "embedded c", "embedded c++",
        "embedded systems", "embedded linux",
        "arm ", "cortex-", "nxp ", "marvell", "stm32",
        "ti ", "texas instruments", "microchip", "renesas",
        "real-time", "realtime", "low-level", "low level",
        "router ", "switch ", "gateway",
        "5g ", "oran", "o-ran", "o-ran", "openran",
        "telecom", "telecommunications", "network equipment",
        "cisco ios", "cisco nx-os", "ios-xr",
        "l2 switch", "l3 router", "layer 2", "layer 3",
        "ptp ", "sync-e", "synce", "ieee 1588",
        "high-speed interface", "throughput optimization",
        "assembly", "assembler",
    ]),
]

# ── IT Noise Patterns ─────────────────────────────────────────────────────────
# Presence of these WITHOUT sufficient silicon/kernel-layer signals
# indicates a pure-software / IT role, not embedded.
# Each noise hit = -8 points.

IT_NOISE_PATTERNS = [
    # Web / Frontend stack
    "react", "angular", "vue", "svelte", "next.js", "nextjs",
    "node.js", "nodejs", "express.js",
    "javascript", "typescript", "css3", "html5",
    "full stack", "fullstack", "frontend", "front-end",
    "web application", "web developer", "web development",
    "rest api", "restful api", "graphql", "microservice",
    # Cloud / DevOps stack
    "aws ", "amazon web services", "azure", "gcp", "google cloud",
    "cloud-native", "cloud native", "serverless",
    "kubernetes", "k8s", "docker swarm", "terraform", "helm chart",
    "devops", "sre", "site reliability engineer",
    "platform engineering", "infrastructure as code",
    "ci/cd pipeline", "jenkins pipeline", "github actions",
    # Pure backend / database
    "mongodb", "postgresql", "mysql", "dynamodb", "cassandra",
    "redis", "elasticsearch", "rabbitmq", "kafka",
    "django", "flask", "spring boot", "fastapi",
    "orm ", "object relational",
    # AI/ML stack (pure software)
    "machine learning", "deep learning", "neural network",
    "llm ", "large language model", "gpt-", "transformer model",
    "nlp ", "natural language processing", "computer vision",
    "tensorflow", "pytorch", "data scientist",
    "ai engineer", "ai/ml", "ml engineer",
    "data pipeline", "data lake", "data warehouse",
    "etl ", "spark", "hadoop",
    "prompt engineer", "ai training", "ai trainer",
    "ai content", "alignerr",  # known AI-training content farm
    # Enterprise / Business stack
    "salesforce", "sap ", "erp ", "dynamics 365",
    ".net", "c#", "power platform", "power apps",
    "lending platform", "fintech", "mortgage", "banking software",
    # Generic IT / Support
    "service desk", "help desk", "it support", "level 2 support",
    "windows server", "active directory",
    "sales engineer", "solutions consultant", "pre-sales",
    "business analyst", "scrum master", "agile coach", "product owner",
    "project manager", "program manager",
]

# ── IT-adjacent tools (neutral) ───────────────────────────────────────────────
# These are used by embedded engineers too — git, docker, python, agile.
# They do NOT count as noise UNLESS they dominate without embedded signals.
TOOLING_TERMS = {
    "git", "docker", "python", "agile", "scrum", "ci/cd", "jira",
    "linux", "c++", "c ", "makefile", "cmake", "gcc", "gdb",
}

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 2 — IC / Lead Dual Track
# ──────────────────────────────────────────────────────────────────────────────
# Classifies each role based on title patterns.
# Senior engineers with 25+ years — both tracks are valid:
#   IC Track: Staff, Principal, Distinguished, Fellow, Senior IC
#   Lead Track: Manager, Director, Head, Team Lead
#   Hybrid: Tech Lead (hands-on + mentoring), "Lead Engineer"
# ═══════════════════════════════════════════════════════════════════════════════

IC_TRACK_PATTERNS = [
    (r'\bprincipal\b', "Principal IC"),
    (r'\bdistinguished\b', "Distinguished IC"),
    (r'\bfellow\b', "Fellow IC"),
    (r'\bstaff\s+(software|embedded|firmware|systems|engineer)', "Staff IC"),
    (r'\bsenior\s+staff\b', "Senior Staff IC"),
    (r'\b(?:senior|sr\.?)\s+(?:embedded|firmware|software|systems)\b', "Senior IC"),
    (r'\bengineer\b(?!.*\b(?:manager|lead|director|head)\b)', "Engineer IC"),
]

LEAD_TRACK_PATTERNS = [
    (r'\b(?:engineering|software|firmware|embedded)\s+(?:director|manager|lead)\b', "Engineering Leader"),
    (r'\bdirector\s+(?:of\s+)?(?:engineering|software|embedded|firmware)', "Director"),
    (r'\bhead\s+of\s+(?:engineering|software|embedded|firmware)', "Head of Engineering"),
    (r'\bteam\s+lead\b', "Team Lead"),
    (r'\btech\s*lead\b', "Tech Lead"),
    (r'\blead\s+(?:embedded|firmware|software|systems|engineer)', "Lead Engineer"),
    (r'\bVP\s+(?:of\s+)?(?:engineering|software|embedded)', "VP Engineering"),
    (r'\bmanager\b', "Manager"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 3 — Australian Security Clearance Red Line
# ──────────────────────────────────────────────────────────────────────────────
# Security clearances (NV1/NV2/Baseline) require citizenship at minimum;
# some Baseline roles may accept PR. These roles should be flagged for review.
#
# References:
#   - AGSVA: Baseline = citizen or PR (some cases). NV1/NV2 = citizen only.
#   - Defence contracts: almost always require at minimum Baseline.
#   - "Must be able to obtain" = still requires citizenship eligibility.
# ═══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate a job REQUIRES security clearance
# These are hard-red for non-citizen/non-PR applicants.
SECURITY_CLEARANCE_PATTERNS = [
    # ── Explicit clearance levels ──
    (r'\bNV\s*1\b', "NV1"),
    (r'\bNV\s*2\b', "NV2"),
    (r'\bNV[- ]?1\b', "NV1"),
    (r'\bNV[- ]?2\b', "NV2"),
    (r'\bNegative\s+Vetting\s+Level\s+[12]\b', "Negative Vetting"),
    (r'\bNegative\s+Vetting\b', "Negative Vetting"),
    (r'\bBaseline\s+(?:Security\s+)?Clearance\b', "Baseline Clearance"),
    (r'\b(?:must\s+hold|requires?|requiring|eligible\s+for|eligibility\s+for)\s+(?:an?\s+)?(?:AGSVA|Australian\s+Government)\s*(?:Security)?\s*Clearance\b',
     "AGSVA Clearance"),
    (r'\b(?:must\s+hold|requires?|requiring)\s+(?:an?\s+)?(?:Australian|Defence)\s*(?:Government|Security)?\s*(?:Security)?\s*Clearance\b',
     "Government Clearance"),
    # ── Generic clearance mentions ──
    # (?<!no )(?<!not ) guards: "No security clearance required" must not match
    (r'(?<!no )(?<!not )Security\s+Clearance\s*(?::|required|must|essential|mandatory|needed)\b',
     "Security Clearance Required"),
    # Tempered dot: don't cross "no"/"not" — "requires no security clearance" must not match
    (r'\b(?:must|requires?|need(?:ed)?|essential)\s+(?:(?!\bno\b|\bnot\b).)*?\bclearances?\b',
     "Clearance Required"),
    (r'\beligible\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:a\s+)?(?:security\s+)?clearances?\b',
     "Must Obtain Clearance"),
    (r'\bability\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:a\s+)?(?:security\s+)?clearances?\b',
     "Must Obtain Clearance"),
    (r'\b(?:must\s+)?(?:be|are)\s+able\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:a\s+)?(?:security\s+)?clearances?\b',
     "Must Obtain Clearance"),
    (r'\bclearances?\s*(?::|is|are)\s*(?:required|essential|mandatory|needed|must|a\s+must)\b',
     "Clearance Required"),
    # ── Defence / Government security ──
    (r'\bAGSVA\b', "AGSVA"),
    (r'\bITAR\b', "ITAR Export Control"),
    (r'\bInternational\s+Traffic\s+in\s+Arms\s+Regulations\b', "ITAR Export Control"),
    (r'\bDefence\s+(?:Security|Clearance|Vetting)\b', "Defence Clearance"),
    (r'\b(?:Australian|Commonwealth)\s+(?:Government|Public\s+Service)\s+(?:Security|Clearance)\b',
     "Government Clearance"),
    (r'\bProtected\s+(?:BASELINE|NV1|NV2)\b', "Protected Clearance"),
    # ── SCEC / physical access ──
    (r'\bSCEC\b', "SCEC"),
]

# Patterns that indicate CITIZENSHIP is required (broader than clearance)
# Non-citizens cannot satisfy these requirements.
CITIZENSHIP_REQUIRED_PATTERNS = [
    (r'\bmust\s+be\s+an?\s+Australian\s+(?:Citizen|citizen)\b', "Citizenship Required"),
    (r'\bAustralian\s+(?:Citizenship|citizenship)\s+(?:required|essential|mandatory|must|is\s+required)\b',
     "Citizenship Required"),
    (r'\b(?:must\s+be|need\s+to\s+be|required\s+to\s+be)\s+(?:an?\s+)?Australian\s+(?:Citizen|citizen)\b',
     "Citizenship Required"),
    (r'\bonly\s+open\s+to\s+Australian\s+(?:Citizens?|citizens?)\b',
     "Citizenship Required"),
    (r'\bAustralian\s+Citizens?\s+Only\b', "Citizenship Required"),
    (r'\bcitizenship\s+(?:required|essential|mandatory|must|is\s+required)\b',
     "Citizenship Required"),
    # Australian Permanent Resident required
    (r'\b(?:Australian\s+)?Permanent\s+Resident\s+(?:required|essential|mandatory|must|only)\b',
     "PR or Citizenship Required"),
    (r'\bmust\s+be\s+(?:an?\s+)?(?:Australian\s+)?Permanent\s+Resident\b',
     "PR or Citizenship Required"),
    (r'\b(?:Citizens?|citizens?|PR|Permanent\s+Resident)\s+(?:and|or)\s+(?:Citizens?|citizens?|PR|Permanent\s+Resident)\b',
     "Citizenship/PR Required"),
    # "full, permanent work rights" excludes visa holders -> equivalent to PR/citizenship
    (r'\b(?:must\s+have|requires?|essential|mandatory|need\s+to\s+have)\s+(?:full\s*,\s*)?permanent\s+work\s+rights\b',
     "PR or Citizenship Required"),
    # AGSVA baseline requires at minimum PR (often citizenship)
    (r'\b(?:must|requires?|need)\s+(?:to\s+)?(?:be\s+(?:able\s+to\s+)?)?(?:eligible\s+(?:for|to\s+apply\s+for)|obtain|hold)\s+(?:a\s+)?(?:AGSVA|Australian\s+Government)\s*(?:Security)?\s*(?:Vetting|Clearance)',
     "AGSVA Required"),
]

# Compile all red-line patterns for efficient scanning
RED_LINE_REGEX = [
    (re.compile(pat, re.IGNORECASE), label)
    for pat, label in SECURITY_CLEARANCE_PATTERNS + CITIZENSHIP_REQUIRED_PATTERNS
]

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 4 — Hardware Design Exclusion (PCB / Circuit Design)
# ──────────────────────────────────────────────────────────────────────────────
# Filters out roles focused on PCB layout, circuit design, EDA tools, and
# electronics hardware design — these are distinct from embedded software /
# firmware engineering and are not a fit for this search.
# ═══════════════════════════════════════════════════════════════════════════════

HARDWARE_DESIGN_EXCLUDE_PATTERNS = [
    # ── PCB Design / Layout ──
    r'\bPCB\b', r'\bPCBA\b',
    r'\bprinted\s+circuit\s+board\b',
    r'\bPCB\s+layout\b',
    r'\bPCB\s+design\b',
    r'\bmultilayer\s+PCB\b',
    r'\bflex\s+PCB\b', r'\brigid-flex\b',
    r'\bsolder\w*\b', r'\bsoldering\b',
    r'\bdesoldering\b',
    r'\brework\s+station\b',
    r'\bSMT\s+(?:assembly|process|pick|mount)\b',
    r'\bsurface\s+mount\b',
    r'\bthrough-hole\b',
    # ── EDA Tools / CAD ──
    r'\bAltium\b', r'\bAltium\s+Designer\b',
    r'\bEagle\b', r'\bKiCad\b',
    r'\bOrCAD\b', r'\bPADS\b',
    r'\bCadence\s+(?:Allegro|OrCAD|Virtuoso)\b',
    r'\bMentor\s+Graphics\b',
    r'\bP\s*CAD\b', r'\bProtel\b',
    r'\bDesignSpark\b', r'\bEasyEDA\b',
    r'\bSPICE\b', r'\bPspice\b', r'\bLTSpice\b',
    r'\bMultisim\b',
    # ── Circuit Design / Analysis ──
    r'\bcircuit\s+design\b',
    r'\bcircuit\s+analysis\b',
    r'\bcircuit\s+simulation\b',
    r'\bschematic\s+capture\b',
    r'\bschematic\s+design\b',
    r'\bcircuit\s+board\b',
    r'\belectronic\s+design\b',
    r'\belectronics\s+design\b',
    r'\belectrical\s+design\b',
    # removed: standalone r'\bhardware\s+design\b' — too many false positives for FW roles
    r'\banalog\s+design\b',
    r'\bdigital\s+design\b',
    r'\bRF\s+design\b',
    r'\bpower\s+electronics\s+design\b',
    r'\bmixed-signal\s+design\b',
    r'\bhigh-speed\s+digital\s+design\b',
    r'\bsignal\s+integrity\s+analysis\b',
    r'\bpower\s+integrity\b',
    r'\bthermal\s+analysis\b',
    r'\bEMC\s+design\b', r'\bEMI\s+mitigation\b',
    r'\belectromagnetic\s+(?:compatibility|interference)\b',
    # ── Test & Measurement (hardware focused) ──
    r'\boscilloscope\s+measurement\b',
    r'\bnodal\s+analysis\b',
    r'\bfrequency\s+response\b',
    r'\bimpedance\s+matching\b',
    # ── Job title patterns ──
    r'\bhardware\s+design\s+engineer\b',
    r'\bPCB\s+(?:designer|layout\s+engineer)\b',
    r'\belectronics\s+design\s+engineer\b',
    r'\belectrical\s+design\s+engineer\b',
    r'\bcircuit\s+design\s+engineer\b',
    r'\blayout\s+engineer\b',
    r'\bCAD\s+engineer\b',
    r'\bhardware\s+test\s+engineer\b',
    r'\bpower\s+electronics\s+engineer\b',
    r'\belectronic\s+engineer\b(?!.*\b(?:firmware|embedded|software|linux)\b)',
    # ── PCB-specific manufacturing ──
    r'\bSMT\s+engineer\b',
]

# Compile once for efficiency
HW_DESIGN_REGEX = [
    re.compile(pat, re.IGNORECASE) for pat in HARDWARE_DESIGN_EXCLUDE_PATTERNS
]


def check_hardware_design_exclusion(title, description=""):
    """Check if a job posting is primarily about PCB / circuit design (hardware
    engineering) rather than embedded software / firmware.

    Returns (is_excluded, matched_pattern, matched_text).

    Smart exclusion:
    - If the title itself contains design terms (PCB, Altium, circuit design),
      the role IS excluded (it's a hardware design role).
    - If only the description matches, we check context:
      * For firmware/embedded software titles, bare 'PCB' in the description
        (e.g. "nice to have: PCB exposure") does NOT trigger exclusion.
      * Design-specific matches (PCB layout, Altium, etc.) in the description
        still trigger exclusion even for firmware titles.
    """
    def check_text(text, skip_bare_pcb=False):
        """Search text against HW_DESIGN_REGEX, optionally skipping bare PCB."""
        for regex in HW_DESIGN_REGEX:
            match = regex.search(text)
            if not match:
                continue
            pattern_str = regex.pattern
            # Skip bare \bPCB\b for firmware roles (nice-to-have context)
            if skip_bare_pcb and pattern_str in (
                r'\bPCB\b',
            ):
                continue
            return True, regex.pattern[:50], match.group(0).strip()
        return False, None, None

    # 1) Check title first — any design term in title → immediate exclusion
    is_excluded, pattern, matched = check_text(title)
    if is_excluded:
        return True, pattern, matched

    # 2) Check if this is a firmware/embedded software role
    is_firmware_role = bool(re.search(
        r'\b(?:embedded\s+(?:software|firmware|systems|engineer|linux)|'
        r'firmware\s+(?:engineer|developer|lead)|'
        r'embedded\s+c(?:\+\+|)|'
        r'rtos|'
        r'bare.?metal)\b',
        title, re.IGNORECASE
    ))

    # 3) Check description — be lenient with bare PCB for firmware roles
    is_excluded, pattern, matched = check_text(description, skip_bare_pcb=is_firmware_role)
    if is_excluded:
        return True, pattern, matched

    return False, None, None


# ═══════════════════════════════════════════════════════════════════════════════
# Scoring Functions
# ═══════════════════════════════════════════════════════════════════════════════

def compute_semantic_depth(title, description, company):
    """Analyze a job posting for genuine embedded depth vs IT noise.

    Returns:
        depth_score: raw signal sum from silicon/kernel/hardware/embedded layers
        noise_count: number of IT-noise patterns found
        depth_signals: list of (layer_name, keyword) found
        noise_signals: list of noise keywords found
        purity_ratio: depth_score / (depth_score + noise_penalty)
    """
    text = f"{title} {description} {company}".lower()
    depth_score = 0
    depth_signals = []
    noise_count = 0
    noise_signals = []

    # ── Smart keyword matcher ───────────────────────────────────────────
    # Short acronyms (≤4 chars, alphanumeric only) use word-boundary regex
    # to avoid substring false positives:
    #   "spi" matching "responsible", "tee" matching "committee".
    # Special-char terms (e.g. "c#", ".net", "gpt-") keep simple 'in' match.
    SHORT_KW_BOUNDARY = 4

    def kw_matches(keyword, text):
        """Match a keyword in text, using word boundaries for short acronyms."""
        kw = keyword.strip()
        if len(kw) <= SHORT_KW_BOUNDARY and kw.isalnum():
            return bool(re.search(r'\b' + re.escape(kw) + r'\b', text))
        else:
            return kw in text

    # ── Compute depth signals ──
    for weight, layer_name, keywords in SEMANTIC_DEPTH_LAYERS:
        for kw in keywords:
            if kw_matches(kw, text):
                depth_score += weight
                depth_signals.append((layer_name, kw))

    # ── Compute IT noise ──
    for kw in IT_NOISE_PATTERNS:
        if kw_matches(kw, text):
            noise_count += 1
            noise_signals.append(kw)

    # ── Context correction ──────────────────────────────────────────────────
    # If "linux" or "c++" appear WITHOUT any silicon/kernel hardware signals,
    # it's likely a pure-software/IT role. Count these as noise.
    silicon_kernel_count = sum(
        1 for layer, _ in depth_signals
        if layer in ("silicon", "kernel")
    )
    hardware_count = sum(
        1 for layer, _ in depth_signals
        if layer in ("silicon", "kernel", "hardware")
    )

    # Tooling terms that appear without hardware context → soft noise
    tooling_in_text = [t for t in TOOLING_TERMS if t in text]
    if hardware_count == 0 and len(tooling_in_text) >= 3:
        # 3+ tooling terms with zero hardware context → IT noise
        noise_count += len(tooling_in_text) - 2

    # AI/ML signals without hardware → strong IT noise
    ai_ml_signals = ["machine learning", "deep learning", "neural network",
                     "llm ", "large language model", "tensorflow", "pytorch",
                     "data scientist", "ai engineer"]
    if any(s in text for s in ai_ml_signals) and hardware_count == 0:
        noise_count += 5  # heavy penalty for pure AI/ML roles

    # ── Embedded context: when hardware depth signals are present ────────────
    # cloud/devops/infra terms are part of the normal embedded toolchain
    # (CI/CD for firmware, IoT cloud backends) — not IT noise.
    EMBEDDED_ADJACENT_NOISE = {
        "azure", "aws ", "devops", "ci/cd pipeline", "jenkins pipeline",
        "github actions", "docker swarm", "kubernetes", "k8s",
        "terraform", "helm chart", "infrastructure as code",
    }
    if hardware_count > 0:
        noise_signals = [n for n in noise_signals if n not in EMBEDDED_ADJACENT_NOISE]
        noise_count = len(noise_signals)

    # ── Compute purity ──────────────────────────────────────────────────────
    noise_penalty = noise_count * 8
    if depth_score + noise_penalty == 0:
        purity_ratio = 0.5
    else:
        purity_ratio = depth_score / (depth_score + noise_penalty)

    return depth_score, noise_count, depth_signals, noise_signals, purity_ratio


def compute_relevance(title, description="", company=""):
    """Enhanced relevance scoring using semantic depth analysis.

    Returns (final_score, depth_score, noise_count, purity, depth_signals,
             noise_signals, embedded_tier).
    """
    depth_score, noise_count, depth_signals, noise_signals, purity = \
        compute_semantic_depth(title, description, company)

    # Final score = depth score minus noise penalty, scaled by purity
    noise_penalty = noise_count * 8
    raw_score = max(depth_score - noise_penalty, 0)

    # Purity scaling: impure results get their score reduced
    if purity < 0.3:
        # Very impure: heavily penalize
        final_score = raw_score * 0.3
    elif purity < 0.5:
        final_score = raw_score * 0.6
    elif purity < 0.7:
        final_score = raw_score * 0.85
    else:
        final_score = raw_score

    # Embedded tier based on highest signal layer found
    layers_found = {layer for layer, _ in depth_signals}
    if "silicon" in layers_found:
        embedded_tier = "SILICON"
    elif "kernel" in layers_found:
        embedded_tier = "KERNEL"
    elif "hardware" in layers_found:
        embedded_tier = "HARDWARE"
    elif "embedded" in layers_found:
        embedded_tier = "EMBEDDED"
    else:
        embedded_tier = "GENERIC"

    return int(final_score), depth_score, noise_count, round(purity, 2), \
        depth_signals, noise_signals, embedded_tier


def get_relevance_tier(score, purity, embedded_tier):
    """Human-readable relevance tier incorporating purity and depth."""
    if embedded_tier == "SILICON" and score >= 25:
        return "🔥🔥 Bare-Metal Gold"
    elif embedded_tier in ("SILICON", "KERNEL") and score >= 20:
        return "🔥 Strong Match"
    elif embedded_tier in ("HARDWARE", "EMBEDDED") and score >= 15:
        return "✅ Good Match"
    elif score >= 10 and purity >= 0.5:
        return "⚠️ Possible Match"
    elif score >= 5:
        return "🤔 Weak Signal"
    else:
        return "❌ IT Noise / Irrelevant"


# ═══════════════════════════════════════════════════════════════════════════════
# Career Track Classification
# ═══════════════════════════════════════════════════════════════════════════════

def assess_career_track(title):
    """Classify a job as IC Track, Lead Track, or Hybrid.

    Returns (track_label, track_detail).
    """
    t = title.lower()
    lead_matches = []
    ic_matches = []

    for pattern, label in LEAD_TRACK_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            lead_matches.append(label)

    for pattern, label in IC_TRACK_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            ic_matches.append(label)

    has_lead = bool(lead_matches)
    has_ic = bool(ic_matches)

    if has_lead and has_ic:
        return "🎯 Hybrid", "; ".join(lead_matches + ic_matches)
    elif has_lead:
        return "👔 Lead Track", "; ".join(lead_matches)
    elif has_ic:
        return "💻 IC Track", "; ".join(ic_matches)
    else:
        # Default: title doesn't clearly indicate — assume IC
        return "💻 IC Track", "Generic Engineer"


# ═══════════════════════════════════════════════════════════════════════════════
# Security Clearance Red Line
# ═══════════════════════════════════════════════════════════════════════════════

def check_security_clearance(title, description=""):
    """Check if a job requires security clearance or citizenship.

    Returns (is_blocked, clearance_level, matched_pattern, detail).
    """
    text = f"{title} {description}"

    for regex, label in RED_LINE_REGEX:
        match = regex.search(text)
        if match:
            # Determine severity and level
            matched_text = match.group(0).strip()

            # Classify the level
            if "NV2" in matched_text.upper() or "NV-2" in matched_text.upper():
                return True, "NV2", label, f"NV2 required: '{matched_text}'"
            elif "NV1" in matched_text.upper() or "NV-1" in matched_text.upper():
                return True, "NV1", label, f"NV1 required: '{matched_text}'"
            elif "BASELINE" in matched_text.upper() or "Baseline" in matched_text:
                return True, "Baseline", label, f"Baseline clearance: '{matched_text}'"
            elif "NEGATIVE VETTING" in matched_text.upper():
                return True, "Negative Vetting", label, f"Negative Vetting: '{matched_text}'"
            elif "CITIZEN" in matched_text.upper():
                return True, "Citizenship", label, f"Citizenship required: '{matched_text}'"
            elif "PERMANENT RESIDENT" in matched_text.upper():
                return True, "PR Required", label, f"PR required: '{matched_text}'"
            elif "AGSVA" in matched_text.upper():
                return True, "AGSVA", label, f"AGSVA clearance: '{matched_text}'"
            elif "SCEC" in matched_text.upper():
                return True, "SCEC", label, f"SCEC access: '{matched_text}'"
            elif "ITAR" in matched_text.upper():
                return True, "ITAR Export Control", label, f"ITAR export control: '{matched_text}'"
            elif "WORK RIGHTS" in matched_text.upper():
                return True, "PR Required", label, f"Permanent work rights required: '{matched_text}'"
            else:
                return True, "Unknown", label, f"Clearance required: '{matched_text}'"

    # Check for negative patterns — explicit "no clearance needed" or "citizenship not required"
    negative_patterns = [
        r'(?:no|not)\s+(?:requiring|require|need|needing)\s+(?:a\s+)?(?:security\s+)?clearance',
        r'\bno\s+(?:security\s+)?clearances?\s+(?:is\s+)?(?:required|needed|necessary)\b',
        r'clearance\s+(?:is\s+)?not\s+(?:required|needed|necessary)',
        r'(?:all|any)\s+(?:visas?|work\s+rights?|work\s+permits?)\s+(?:welcome|accepted|considered)',
    ]
    for pat in negative_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return False, "None", "Explicitly Open", "No clearance required"

    return False, "None", "None", "No clearance or citizenship restriction detected"


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    all_jobs = []

    print("=" * 72)
    print("🔍 Embedded Systems Job Search")
    print(f"   🧠 Semantic Depth Brain: ACTIVE")
    print(f"   🛤️  IC / Lead Dual Track: ACTIVE")
    print(f"   🚫 Security Clearance Red Line: ACTIVE")
    print(f"   🔧 PCB/Circuit Design Filter: ACTIVE")
    print(f"   📍 Location: {LOCATION}")
    print(f"   🔎 Search terms: {len(SEARCH_TERMS)}")
    print(f"   ⏱️  Max age: {HOURS_OLD}h ({HOURS_OLD // 24} days)")
    print("=" * 72)

    # ── Scrape ──────────────────────────────────────────────────────────────
    for term in SEARCH_TERMS:
        print(f"\n🔎 Searching: '{term}' ...")
        try:
            jobs = scrape_jobs(
                site_name=["linkedin", "indeed", "google", "seek"],
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

    # ── Combine & Deduplicate ───────────────────────────────────────────────
    import pandas as pd

    if not all_jobs:
        print("\n❌ No results found.")
        return

    combined = pd.concat(all_jobs, ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["job_url"], keep="first")
    print(f"\n📊 Combined: {before} → {len(combined)} after URL dedup")

    # ── Melbourne Location Filter ────────────────────────────────────────────
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

    # ── Enhanced Analysis (all 3 engines) ────────────────────────────────────
    print(f"\n🧠 Running Semantic Depth + Career Track + Clearance analysis...")

    results = []
    for _, row in combined.iterrows():
        title = str(row.get("title", ""))
        company = str(row.get("company", ""))
        desc = str(row.get("description", ""))

        # Engine 1: Semantic Depth
        score, depth_raw, noise_n, purity, depth_sigs, noise_sigs, emb_tier = \
            compute_relevance(title, desc, company)

        # Engine 2: Career Track
        track_label, track_detail = assess_career_track(title)

        # Engine 3: Security Clearance
        is_blocked, clearance_level, clearance_label, clearance_detail = \
            check_security_clearance(title, desc)

        # Engine 4: Hardware Design Exclusion (PCB / Circuit Design)
        is_hw_design, hw_design_pat, hw_design_matched = \
            check_hardware_design_exclusion(title, desc)

        depth_keywords = ", ".join([kw for _, kw in depth_sigs[:6]])
        noise_keywords = ", ".join(noise_sigs[:5])

        # Format salary string
        min_sal = row.get("min_amount")
        max_sal = row.get("max_amount")
        currency = str(row.get("currency", "") or "")
        interval = str(row.get("interval", "") or "")
        sal_str = ""
        if pd.notna(min_sal) and pd.notna(max_sal) and min_sal and max_sal:
            if min_sal >= 1000:
                sal_str = f"{currency}${min_sal/1000:.0f}k–${max_sal/1000:.0f}k"
            else:
                sal_str = f"{currency}${min_sal:.0f}–${max_sal:.0f}"
            if interval and interval != "yearly":
                sal_str += f"/{interval}"

        results.append({
            "title": title,
            "company": company,
            "location": str(row.get("location", "")),
            "date_posted": str(row.get("date_posted", "")),
            "job_url": str(row.get("job_url", "")),
            "site": str(row.get("site", "")),
            "description": desc,
            # Scoring
            "relevance_score": score,
            "depth_raw": depth_raw,
            "noise_count": noise_n,
            "semantic_purity": purity,
            "embedded_tier": emb_tier,
            "relevance_tier": get_relevance_tier(score, purity, emb_tier),
            "depth_keywords": depth_keywords,
            "noise_keywords": noise_keywords,
            # Career track
            "career_track": track_label,
            "track_detail": track_detail,
            # Security clearance
            "clearance_blocked": is_blocked,
            "clearance_level": clearance_level,
            "clearance_label": clearance_label,
            "clearance_detail": clearance_detail,
            # Hardware design exclusion
            "hw_design_excluded": is_hw_design,
            "hw_design_detail": f"PCB/Circuit Design: '{hw_design_matched}'" if is_hw_design else "",
            # Salary
            "interval": row.get("interval", None),
            "min_amount": row.get("min_amount", None),
            "max_amount": row.get("max_amount", None),
            "currency": row.get("currency", None),
            "salary_source": row.get("salary_source", None),
            "salary_str": sal_str,
        })

    df = pd.DataFrame(results)
    total = len(df)

    # ── Security Red Line: separate blocked jobs ─────────────────────────────
    blocked = df[df["clearance_blocked"] == True].copy()
    eligible = df[df["clearance_blocked"] == False].copy()

    if len(blocked) > 0:
        print(f"\n{'=' * 72}")
        print(f"🚫 SECURITY CLEARANCE RED LINE — {len(blocked)} jobs BLOCKED")
        print(f"   These require citizenship/PR/clearance — verify your eligibility")
        print(f"{'=' * 72}")
        for _, row in blocked.iterrows():
            print(f"   [{row['clearance_level']:>12s}] {row['title'][:55]} @ {row['company'][:25]}")
            print(f"                      ↳ {row['clearance_detail'][:80]}")

    print(f"\n   ✅ Eligible jobs (no clearance barrier): {len(eligible)}")

    # ── Hardware Design Exclusion: filter out PCB / circuit design roles ──────
    eligible_before_hw = len(eligible)
    hw_design_jobs = eligible[eligible["hw_design_excluded"] == True].copy()
    eligible = eligible[eligible["hw_design_excluded"] == False].copy()

    if len(hw_design_jobs) > 0:
        print(f"\n{'=' * 72}")
        print(f"🔧 HARDWARE DESIGN EXCLUSION — {len(hw_design_jobs)} PCB/circuit design jobs REMOVED")
        print(f"{'=' * 72}")
        for _, row in hw_design_jobs.iterrows():
            print(f"   [{row['relevance_score']:2d}] {row['title'][:55]} @ {row['company'][:25]}")
            print(f"                      ↳ {row['hw_design_detail'][:80]}")

    print(f"   💻 Software-oriented jobs remaining: {len(eligible)}")

    # ── Relevance filter (on eligible + non-hw-design jobs only) ──────────────
    MIN_SCORE = 5
    filtered = eligible[eligible["relevance_score"] >= MIN_SCORE].copy()
    dropped = eligible[eligible["relevance_score"] < MIN_SCORE]

    if len(dropped) > 0:
        print(f"\n🗑️  Dropped {len(dropped)} low-relevance eligible jobs (score < {MIN_SCORE}):")
        for _, row in dropped.iterrows():
            t = str(row.get('title', ''))[:60]
            c = str(row.get('company', ''))[:25]
            s = int(row.get('relevance_score', 0))
            print(f"    [{s:2d}] {t} @ {c}")

    print(f"🎯 Final pool: {len(filtered)} high-value, legally-eligible jobs")

    # ── Sort: relevance score first, then date ───────────────────────────────
    if "date_posted" in filtered.columns:
        filtered = filtered.sort_values(
            ["relevance_score", "date_posted"],
            ascending=[False, False]
        )

    # ── Save CSV ────────────────────────────────────────────────────────────
    csv_cols = [
        "relevance_tier", "embedded_tier", "career_track",
        "title", "company", "location", "date_posted", "salary_str",
        "relevance_score", "semantic_purity", "depth_raw", "noise_count",
        "depth_keywords", "noise_keywords",
        "track_detail", "clearance_detail", "job_url", "site"
    ]
    csv_cols = [c for c in csv_cols if c in filtered.columns]
    filtered[csv_cols].to_csv(OUTPUT_CSV, index=False, quoting=csv.QUOTE_NONNUMERIC)
    print(f"\n💾 CSV saved to: {OUTPUT_CSV}")

    # ── Terminal Summary ────────────────────────────────────────────────────
    display_cols = [
        "relevance_tier", "embedded_tier", "career_track",
        "title", "company", "salary_str", "date_posted",
        "relevance_score", "semantic_purity", "depth_keywords"
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]
    print(f"\n{'=' * 72}")
    print(f"🏆 Top Results (sorted by relevance)")
    print(f"{'=' * 72}")
    pd.set_option('display.max_colwidth', 60)
    pd.set_option('display.width', 200)
    print(filtered[display_cols].head(30).to_string(index=False))

    # ── Summary Stats ────────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"📈 Summary")

    # By relevance tier
    print(f"   ── By Relevance Tier ──")
    tier_counts = filtered["relevance_tier"].value_counts()
    tier_order = ["🔥🔥 Bare-Metal Gold", "🔥 Strong Match", "✅ Good Match",
                  "⚠️ Possible Match", "🤔 Weak Signal", "❌ IT Noise / Irrelevant"]
    for tier in tier_order:
        count = tier_counts.get(tier, 0)
        if count > 0:
            print(f"      {tier}: {count}")

    # By embedded depth
    print(f"   ── By Embedded Depth ──")
    depth_counts = filtered["embedded_tier"].value_counts()
    for d in ["SILICON", "KERNEL", "HARDWARE", "EMBEDDED", "GENERIC"]:
        count = depth_counts.get(d, 0)
        if count > 0:
            print(f"      {d}: {count}")

    # By career track
    print(f"   ── By Career Track ──")
    track_counts = filtered["career_track"].value_counts()
    for t, count in track_counts.items():
        print(f"      {t}: {count}")

    # Clearance summary
    print(f"   ── Security Clearance ──")
    print(f"      🚫 Blocked (NV1/NV2/Baseline/Citizenship): {len(blocked)}")
    print(f"      ✅ Eligible (no clearance barrier):          {eligible_before_hw}")

    # Hardware design exclusion summary
    print(f"   ── Hardware Design Exclusion ──")
    print(f"      🔧 PCB/Circuit Design roles removed: {len(hw_design_jobs)}")
    print(f"      💻 Software/Firmware roles kept:     {len(eligible)}")

    # ── Kanban JSON ──────────────────────────────────────────────────────────
    kanban_file = os.path.join(OUTPUT_DIR, f"kanban_jobs_{timestamp}.json")
    kanban_entries = []

    for _, row in filtered.head(40).iterrows():
        entry = {
            "title": str(row.get("title", "")),
            "company": str(row.get("company", "")),
            "location": str(row.get("location", "")),
            "date_posted": str(row.get("date_posted", "")),
            "url": str(row.get("job_url", "")),
            "source": str(row.get("site", "")),
            # Salary
            "salary": str(row.get("salary_str", "")),
            # Semantic depth
            "relevance": str(row.get("relevance_tier", "")),
            "embedded_tier": str(row.get("embedded_tier", "")),
            "score": int(row.get("relevance_score", 0)),
            "purity": float(row.get("semantic_purity", 0)),
            "matched_on": str(row.get("depth_keywords", "")),
            "noise_warning": str(row.get("noise_keywords", "")) if row.get("noise_count", 0) > 0 else "",
            # Career track
            "career_track": str(row.get("career_track", "")),
            "track_detail": str(row.get("track_detail", "")),
            # Clearance
            "clearance_status": "✅ No Clearance Required" if not row.get("clearance_blocked") else f"🚫 BLOCKED: {row.get('clearance_level', '')}",
            # Kanban
            "status": "New",
            "notes": (
                f"Tier={row.get('embedded_tier', '')} | "
                f"Score={int(row.get('relevance_score', 0))} | "
                f"Purity={float(row.get('semantic_purity', 0)):.0%} | "
                f"{row.get('track_detail', '')}"
                f"{' | 💰 ' + str(row.get('salary_str', '')) if row.get('salary_str') else ''}"
            ),
        }
        kanban_entries.append(entry)

    with open(kanban_file, "w", encoding="utf-8") as f:
        json.dump(kanban_entries, f, indent=2, ensure_ascii=False)
    print(f"\n📋 Kanban JSON saved to: {kanban_file} ({len(kanban_entries)} entries)")

    # ── Also save blocked jobs to a separate file for reference ──────────────
    if len(blocked) > 0:
        blocked_file = os.path.join(OUTPUT_DIR, f"blocked_clearance_jobs_{timestamp}.json")
        blocked_entries = []
        for _, row in blocked.iterrows():
            blocked_entries.append({
                "title": str(row.get("title", "")),
                "company": str(row.get("company", "")),
                "url": str(row.get("job_url", "")),
                "clearance_level": str(row.get("clearance_level", "")),
                "detail": str(row.get("clearance_detail", "")),
            })
        with open(blocked_file, "w", encoding="utf-8") as f:
            json.dump(blocked_entries, f, indent=2, ensure_ascii=False)
        print(f"📋 Blocked clearance jobs saved to: {blocked_file} ({len(blocked_entries)} entries)")

    print(f"\n✅ Done! Run again with: python linkedin_job_search.py")


if __name__ == "__main__":
    main()
