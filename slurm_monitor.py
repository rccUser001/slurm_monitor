#!/usr/bin/env python3
"""
slurm_monitor — Per-job resource waste analysis for your Slurm jobs.

Focus: quantify exactly how much CPU time, memory, wall time, and GPU·hours
were allocated but unused — so you can right-size your next job request.

No external dependencies. Reads only standard Slurm commands (sacct, squeue).

Usage (module):
  slurm_monitor                        # current user, last 7 days
  slurm_monitor -d 30                  # last 30 days
  slurm_monitor -u <username>          # another user (if permitted)
  slurm_monitor -n 50                  # show last 50 completed jobs
  slurm_monitor --gpu-only             # only show GPU jobs
  slurm_monitor --no-color             # plain text (for piping/logging)
  slurm_monitor --tips-only            # only print actionable tips

Usage (direct):
  python3 slurm_monitor.py             # same options apply
"""

import subprocess
import sys
import argparse
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Terminal colors
# ---------------------------------------------------------------------------

class C:
    BOLD   = '\033[1m'
    DIM    = '\033[2m'
    RED    = '\033[91m'
    YELLOW = '\033[93m'
    GREEN  = '\033[92m'
    CYAN   = '\033[96m'
    BLUE   = '\033[94m'
    MAGENTA = '\033[95m'
    RESET  = '\033[0m'

_color_enabled = True

def _c(code):
    return code if _color_enabled else ''

def bold(s):    return f"{_c(C.BOLD)}{s}{_c(C.RESET)}"
def dim(s):     return f"{_c(C.DIM)}{s}{_c(C.RESET)}"
def red(s):     return f"{_c(C.RED)}{s}{_c(C.RESET)}"
def yellow(s):  return f"{_c(C.YELLOW)}{s}{_c(C.RESET)}"
def green(s):   return f"{_c(C.GREEN)}{s}{_c(C.RESET)}"
def cyan(s):    return f"{_c(C.CYAN)}{s}{_c(C.RESET)}"
def blue(s):    return f"{_c(C.BLUE)}{s}{_c(C.RESET)}"
def magenta(s): return f"{_c(C.MAGENTA)}{s}{_c(C.RESET)}"


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return r.stdout.decode().strip()
    except Exception:
        return ""


def whoami():
    return run("whoami")


def header(title):
    line = "─" * 110
    print(f"\n{bold(cyan(line))}")
    print(bold(cyan(f"  {title}")))
    print(bold(cyan(line)))


# ---------------------------------------------------------------------------
# Efficiency display helpers
# ---------------------------------------------------------------------------

def eff_color(pct):
    if pct is None:
        return dim("  N/A")
    s = f"{pct:5.1f}%"
    if pct >= 75:
        return green(s)
    elif pct >= 40:
        return yellow(s)
    else:
        return red(s)


def eff_bar(pct, width=20):
    if pct is None:
        return dim("─" * width)
    filled = int(round(min(pct, 100) / 100 * width))
    bar = "█" * filled + "░" * (width - filled)
    if pct >= 75:
        return green(bar)
    elif pct >= 40:
        return yellow(bar)
    else:
        return red(bar)


def waste_flag(pct):
    """Return a short waste severity tag."""
    if pct is None:
        return ""
    waste = 100 - pct
    if waste >= 60:
        return red("  ▲ HIGH WASTE")
    elif waste >= 25:
        return yellow("  ▲ moderate waste")
    return ""


# ---------------------------------------------------------------------------
# Unit parsers
# ---------------------------------------------------------------------------

def time_to_sec(t):
    """'D-HH:MM:SS', 'HH:MM:SS', 'MM:SS' → seconds (None on failure)."""
    if not t or t in ("UNLIMITED", "N/A", "Partition_Limit", ""):
        return None
    total = 0
    if "-" in t:
        d, t = t.split("-", 1)
        try:
            total += int(d) * 86400
        except ValueError:
            return None
    parts = t.split(":")
    try:
        if len(parts) == 3:
            total += int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            total += int(parts[0]) * 60 + int(parts[1])
        else:
            total += int(parts[0])
    except ValueError:
        return None
    return total


def sec_to_hms(s):
    if s is None:
        return "N/A"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def sec_to_hours(s):
    if s is None:
        return None
    return s / 3600.0


def mem_to_mb(s):
    """'2048K', '512M', '4G', '1T' → float MB (None on failure)."""
    if not s or s in ("N/A", "0", ""):
        return None
    s = s.strip()
    try:
        if s[-1].upper() == "K":
            return float(s[:-1]) / 1024
        elif s[-1].upper() == "M":
            return float(s[:-1])
        elif s[-1].upper() == "G":
            return float(s[:-1]) * 1024
        elif s[-1].upper() == "T":
            return float(s[:-1]) * 1024 * 1024
        else:
            return float(s) / 1024
    except ValueError:
        return None


def fmt_mb(mb):
    if mb is None:
        return "N/A"
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    return f"{mb:.0f} MB"


def fmt_gpu_hours(h):
    if h is None:
        return "N/A"
    return f"{h:.2f} GPU·hr"


def parse_req_mem(req_mem_str):
    """sacct ReqMem: '4Gn' (per-node) or '2Gc' (per-core). Returns (mb, suffix)."""
    if not req_mem_str:
        return None, None
    s = req_mem_str.strip()
    if s and s[-1].lower() in ("n", "c"):
        return mem_to_mb(s[:-1]), s[-1].lower()
    return mem_to_mb(s), "n"


# ---------------------------------------------------------------------------
# TRES parsing — extracts GPU info from sacct AllocTRES / TRESUsageInTot
# ---------------------------------------------------------------------------

def parse_tres(tres_str):
    """
    Parse a Slurm TRES string into a dict.

    Examples:
      'billing=8,cpu=8,gres/gpu=2,mem=32G,node=1'
        → {'billing': '8', 'cpu': '8', 'gres/gpu': '2', 'mem': '32G', 'node': '1'}

      'cpu=28800,energy=0,gres/gpu=0=7200,gres/gpu=1=7200'
        → {'cpu': '28800', 'energy': '0', 'gres/gpu=0': '7200', 'gres/gpu=1': '7200'}
    """
    if not tres_str or tres_str.strip() in ("", "N/A"):
        return {}
    result = {}
    for token in tres_str.split(","):
        token = token.strip()
        if not token:
            continue
        # Split on the LAST '=' so 'gres/gpu=0=7200' → key='gres/gpu=0', val='7200'
        eq = token.rfind("=")
        if eq == -1:
            continue
        key = token[:eq].strip()
        val = token[eq + 1:].strip()
        result[key] = val
    return result


def gpu_count_from_tres(alloc_tres_str):
    """Return number of GPUs allocated, or 0 if none / not tracked."""
    t = parse_tres(alloc_tres_str)
    raw = t.get("gres/gpu", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def gpu_hours_allocated(gpu_count, elapsed_s):
    """GPU·hours allocated = GPUs × elapsed (what was billed/reserved)."""
    if gpu_count and elapsed_s:
        return gpu_count * elapsed_s / 3600.0
    return None


def gpu_seconds_from_usage_tres(usage_tres_str):
    """
    Sum GPU·seconds from TRESUsageInTot.

    Handles both formats:
      'gres/gpu=7200'            → 7200  (aggregate)
      'gres/gpu=0=3600,gres/gpu=1=3600'  → 7200  (per-device)
    Returns None if no GPU keys found.
    """
    t = parse_tres(usage_tres_str)
    total = 0
    found = False
    for key, val in t.items():
        if key == "gres/gpu" or key.startswith("gres/gpu="):
            try:
                total += int(val)
                found = True
            except ValueError:
                pass
    return total if found else None


# ---------------------------------------------------------------------------
# Queue snapshot (replaces full running-jobs table — live detail belongs
# in a live monitoring tool or `sstat`/`squeue` directly)
# ---------------------------------------------------------------------------

def show_queue_snapshot(user):
    """One-line summary of pending/running counts — no duplicate of squeue."""
    out = run(f"squeue -u {user} -h -o '%T' 2>/dev/null")
    if not out:
        print(dim(f"  Queue: no jobs currently queued for {user}"))
        return

    counts = {}
    for line in out.splitlines():
        s = line.strip()
        counts[s] = counts.get(s, 0) + 1

    parts = []
    for state in ("RUNNING", "PENDING", "COMPLETING", "SUSPENDED"):
        if state in counts:
            n = counts[state]
            col = green if state == "RUNNING" else yellow
            parts.append(col(f"{n} {state.lower()}"))
    for state, n in counts.items():
        if state not in ("RUNNING", "PENDING", "COMPLETING", "SUSPENDED"):
            parts.append(dim(f"{n} {state.lower()}"))

    summary = "  Queue now:  " + "   ".join(parts) if parts else "  Queue: empty"
    print(summary)
    print(dim("  (for live per-process detail, run directly on the compute node or use squeue)"))


# ---------------------------------------------------------------------------
# Per-job waste detail
# ---------------------------------------------------------------------------

_SACCT_FMT = (
    "JobID,JobName%24,State,Partition,AllocCPUS,ReqMem,"
    "MaxRSS,AveRSS,Elapsed,Timelimit,CPUTime,TotalCPU,ExitCode,"
    "NodeList,MaxDiskRead,MaxDiskWrite,"
    "AllocTRES%120,TRESUsageInTot%120"
)


def fetch_jobs(user, days, max_jobs):
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = run(
        f"sacct -u {user} --starttime={start} --endtime=now "
        f"--format={_SACCT_FMT} "
        "--noheader --parsable2 -X"
    )
    if not raw:
        return []

    jobs = []
    for line in raw.splitlines():
        p = line.split("|")
        if len(p) < 16:
            continue
        job_id = p[0].strip()
        if "." in job_id:
            continue
        jobs.append({
            "id":           job_id,
            "name":         p[1][:24].strip(),
            "state":        p[2].strip(),
            "partition":    p[3].strip(),
            "cpus":         p[4].strip(),
            "req_mem":      p[5].strip(),
            "max_rss":      p[6].strip(),
            "ave_rss":      p[7].strip(),
            "elapsed":      p[8].strip(),
            "timelimit":    p[9].strip(),
            "cpu_time":     p[10].strip(),
            "total_cpu":    p[11].strip(),
            "exitcode":     p[12].strip(),
            "nodes":        p[13].strip(),
            "disk_r":       p[14].strip() if len(p) > 14 else "",
            "disk_w":       p[15].strip() if len(p) > 15 else "",
            "alloc_tres":   p[16].strip() if len(p) > 16 else "",
            "tres_usage":   p[17].strip() if len(p) > 17 else "",
        })

    return jobs[-max_jobs:]


def _state_color(state):
    if state == "COMPLETED":
        return green
    if state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"):
        return red
    if "CANCEL" in state:
        return yellow
    return dim


def _print_job_detail(j, tips_only=False):
    state  = j["state"]
    sc     = _state_color(state)
    cpus   = int(j["cpus"]) if j["cpus"].isdigit() else None

    # ── parsed values ──────────────────────────────────────────────────────
    elapsed_s   = time_to_sec(j["elapsed"])
    timelimit_s = time_to_sec(j["timelimit"])
    cpu_time_s  = time_to_sec(j["cpu_time"])
    total_cpu_s = time_to_sec(j["total_cpu"])
    req_mb, req_suffix = parse_req_mem(j["req_mem"])
    max_rss_mb  = mem_to_mb(j["max_rss"])
    ave_rss_mb  = mem_to_mb(j["ave_rss"])
    gpu_count   = gpu_count_from_tres(j["alloc_tres"])

    total_req_mb = req_mb
    if req_suffix == "c" and cpus and req_mb:
        total_req_mb = req_mb * cpus

    # ── efficiencies ───────────────────────────────────────────────────────
    time_eff  = (elapsed_s / timelimit_s * 100)  if elapsed_s and timelimit_s  else None
    cpu_eff   = (total_cpu_s / cpu_time_s * 100) if cpu_time_s and total_cpu_s else None
    mem_eff   = (max_rss_mb / total_req_mb * 100) if max_rss_mb and total_req_mb else None

    # ── waste quantities ───────────────────────────────────────────────────
    wasted_time_s  = (timelimit_s - elapsed_s)   if elapsed_s and timelimit_s  else None
    wasted_cpu_s   = (cpu_time_s  - total_cpu_s) if cpu_time_s and total_cpu_s else None
    wasted_mem_mb  = (total_req_mb - max_rss_mb)  if max_rss_mb and total_req_mb else None
    idle_cores     = (wasted_cpu_s / elapsed_s)   if wasted_cpu_s is not None and elapsed_s else None

    gpu_hrs_alloc  = gpu_hours_allocated(gpu_count, elapsed_s)
    gpu_hrs_wasted = gpu_hours_allocated(gpu_count, wasted_time_s) if gpu_count else None

    # ── collect per-job tips ───────────────────────────────────────────────
    tips = []

    if time_eff is not None and time_eff < 75:
        waste_h = sec_to_hours(wasted_time_s)
        gpu_note = (f", freeing {gpu_hrs_wasted:.1f} GPU·hrs" if gpu_hrs_wasted and gpu_hrs_wasted > 0.1 else "")
        tips.append((
            "wall time",
            f"Used {time_eff:.0f}% of --time limit "
            f"({sec_to_hms(wasted_time_s)} wasted{gpu_note}). "
            f"Set --time to ~{sec_to_hms(int(elapsed_s * 1.2)) if elapsed_s else 'N/A'} "
            f"(+20% buffer).",
            "yellow" if time_eff >= 40 else "red"
        ))

    if cpu_eff is not None and cpu_eff < 75 and cpus and cpus > 1:
        tips.append((
            "CPU cores",
            f"Only {cpu_eff:.0f}% CPU utilization — ~{idle_cores:.1f} of {cpus} cores idle. "
            f"Try --cpus-per-task={max(1, int(idle_cores and cpus - idle_cores + 1 or cpus))}.",
            "yellow" if cpu_eff >= 40 else "red"
        ))

    if mem_eff is not None and mem_eff < 75 and total_req_mb:
        safe_req = max_rss_mb * 1.25 if max_rss_mb else None
        tips.append((
            "memory",
            f"Peak RAM was {mem_eff:.0f}% of request ({wasted_mem_mb and fmt_mb(wasted_mem_mb) or 'N/A'} unused). "
            f"Try --mem={fmt_mb(safe_req)} (+25% buffer)." if safe_req else
            f"Peak RAM was {mem_eff:.0f}% of request. Consider reducing --mem.",
            "yellow" if mem_eff >= 40 else "red"
        ))

    if gpu_count and gpu_hrs_wasted and gpu_hrs_wasted > 0.25:
        tips.append((
            "GPU time",
            f"{gpu_count} GPU(s) allocated but {gpu_hrs_wasted:.1f} GPU·hrs went unused "
            f"(unused wall time × GPU count). Reduce --time to recover them sooner.",
            "yellow" if (time_eff or 0) >= 40 else "red"
        ))

    if state in ("FAILED", "OUT_OF_MEMORY"):
        tips.append((
            "job failure",
            f"Job ended with {state} (exit {j['exitcode']}). "
            + ("Increase --mem; job was killed by OOM." if state == "OUT_OF_MEMORY"
               else "Check logs for the root cause before re-submitting."),
            "red"
        ))

    # ── if tips_only mode, print just the summary line + tips ──────────────
    if tips_only:
        if tips:
            print(f"\n  {bold(j['id'])}  {j['name'][:22]}  {sc(state)}")
            for resource, msg, severity in tips:
                col = red if severity == "red" else yellow
                print(f"    {col('▲')} {bold(resource)}: {msg}")
        return

    # ── job header ─────────────────────────────────────────────────────────
    print(f"\n  {bold(j['id'])}  {bold(j['name'])}  {sc(state)}  "
          f"partition={j['partition']}  nodes={j['nodes']}  exit={j['exitcode']}")
    print(f"  {'─' * 106}")

    # ── wall time ──────────────────────────────────────────────────────────
    print(f"  {'Wall time:':<18} "
          f"requested={bold(sec_to_hms(timelimit_s)):>12}   "
          f"used={bold(sec_to_hms(elapsed_s)):>12}   "
          f"wasted={bold(sec_to_hms(wasted_time_s)):>12}   "
          f"time used={eff_color(time_eff)}  {eff_bar(time_eff)}{waste_flag(time_eff)}")

    # ── CPU ────────────────────────────────────────────────────────────────
    cpu_detail = ""
    if cpus:
        cpu_detail = f"({cpus} cores"
        if idle_cores is not None:
            cpu_detail += f", ~{idle_cores:.1f} idle"
        cpu_detail += ")"

    print(f"  {'CPU time:':<18} "
          f"allocated={bold(sec_to_hms(cpu_time_s)):>12}   "
          f"used={bold(sec_to_hms(total_cpu_s)):>12}   "
          f"wasted={bold(sec_to_hms(wasted_cpu_s)):>12}   "
          f"cpu util={eff_color(cpu_eff)}  {eff_bar(cpu_eff)}{waste_flag(cpu_eff)}"
          f"  {dim(cpu_detail)}")

    # ── memory ─────────────────────────────────────────────────────────────
    req_label = fmt_mb(total_req_mb)
    if req_suffix == "c" and cpus:
        req_label += f" ({fmt_mb(req_mb)}×{cpus} cores)"

    print(f"  {'Memory (RAM):':<18} "
          f"requested={bold(req_label):>18}   "
          f"peak={bold(fmt_mb(max_rss_mb)):>12}   "
          f"avg={bold(fmt_mb(ave_rss_mb)):>12}   "
          f"wasted={bold(fmt_mb(wasted_mem_mb)):>12}   "
          f"mem util={eff_color(mem_eff)}  {eff_bar(mem_eff)}{waste_flag(mem_eff)}")

    # ── GPU ────────────────────────────────────────────────────────────────
    if gpu_count:
        gpu_eff_note = ""
        if gpu_hrs_wasted and gpu_hrs_wasted > 0:
            gpu_eff_note = f"   wasted≈{bold(fmt_gpu_hours(gpu_hrs_wasted))}"
            if time_eff is not None:
                gpu_eff_note += dim(f"  (wall-time waste × {gpu_count} GPU)")
        print(f"  {'GPU:':<18} "
              f"allocated={bold(str(gpu_count) + ' GPU(s)'):>12}   "
              f"GPU·hrs allocated={bold(fmt_gpu_hours(gpu_hrs_alloc)):>14}"
              f"{gpu_eff_note}")
    elif "gres" in j["alloc_tres"].lower() or "gpu" in j["alloc_tres"].lower():
        print(f"  {'GPU:':<18} {dim('TRES contains GPU reference but count is 0 — TRES accounting may be disabled')}")

    # ── disk I/O (informational only) ──────────────────────────────────────
    if j["disk_r"] not in ("", "0") or j["disk_w"] not in ("", "0"):
        print(f"  {'Disk I/O:':<18} read={j['disk_r']}   write={j['disk_w']}")

    # ── SUs charged ────────────────────────────────────────────────────────
    billing_val = parse_tres(j.get("alloc_tres", "")).get("billing")
    if billing_val and elapsed_s:
        try:
            su = int(billing_val) * elapsed_s / 3600.0
            elapsed_h = elapsed_s / 3600.0
            print(f"  {'SUs charged:':<18} {bold(f'{su:.2f}'):>12}  {dim(f'(billing={billing_val} × {elapsed_h:.2f} hrs)')}")
        except ValueError:
            pass

    # ── per-job tips ───────────────────────────────────────────────────────
    if tips:
        print(f"  {'─' * 60}")
        for resource, msg, severity in tips:
            col = red if severity == "red" else yellow
            print(f"  {col('▲')} {bold(resource + ':')} {msg}")


def _print_legend():
    print(f"\n  {bold('Utilization color scale:')}  "
          f"{green('■ ≥75% good')}   "
          f"{yellow('■ ≥40% moderate waste')}   "
          f"{red('■ <40% high waste')}")
    print()


# ---------------------------------------------------------------------------
# Completed job history
# ---------------------------------------------------------------------------

def show_history(user, days, max_jobs, gpu_only=False, tips_only=False):
    label = "GPU JOBS" if gpu_only else "ALL JOBS"
    header(f"WASTE ANALYSIS — {label}  |  {user}  |  last {days} days  |  up to {max_jobs} jobs")

    jobs = fetch_jobs(user, days, max_jobs)

    if gpu_only:
        jobs = [j for j in jobs if gpu_count_from_tres(j["alloc_tres"]) > 0]

    if not jobs:
        print(dim(f"  No completed jobs found in the last {days} days.\n"))
        return []

    for j in jobs:
        _print_job_detail(j, tips_only=tips_only)

    if not tips_only:
        _print_legend()

    return jobs


# ---------------------------------------------------------------------------
# Aggregate waste summary
# ---------------------------------------------------------------------------

def show_summary(user, days, jobs):
    header(f"AGGREGATE WASTE SUMMARY  —  {user}  |  last {days} days")

    if not jobs:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        raw = run(
            f"sacct -u {user} --starttime={start} "
            f"--format=State,AllocCPUS,CPUTime,TotalCPU,ReqMem,MaxRSS,Elapsed,Timelimit,AllocTRES%80 "
            "--noheader --parsable2 -X"
        )
        if not raw:
            print(dim("  No data.\n"))
            return
        jobs = []
        for line in raw.splitlines():
            p = line.split("|")
            if len(p) < 9 or "." in p[0]:
                continue
            jobs.append({
                "state": p[0].strip(), "cpus": p[1].strip(),
                "cpu_time": p[2].strip(), "total_cpu": p[3].strip(),
                "req_mem": p[4].strip(), "max_rss": p[5].strip(),
                "elapsed": p[6].strip(), "timelimit": p[7].strip(),
                "alloc_tres": p[8].strip(),
            })

    counts = {}
    total_cpu_alloc = total_cpu_used  = 0.0
    total_mem_req   = total_mem_used  = 0.0
    total_gpu_hrs_alloc = total_gpu_hrs_wasted = 0.0
    total_sus = 0.0
    gpu_job_count = 0

    for j in jobs:
        state = j["state"]
        counts[state] = counts.get(state, 0) + 1

        ct  = time_to_sec(j.get("cpu_time", ""))
        tc  = time_to_sec(j.get("total_cpu", ""))
        rm, rs = parse_req_mem(j.get("req_mem", ""))
        rss = mem_to_mb(j.get("max_rss", ""))
        n_cpus = int(j["cpus"]) if j.get("cpus", "").isdigit() else 1
        total_req_mb = rm * n_cpus if rm and rs == "c" else rm
        elapsed_s   = time_to_sec(j.get("elapsed", ""))
        timelimit_s = time_to_sec(j.get("timelimit", ""))
        gpu_n = gpu_count_from_tres(j.get("alloc_tres", ""))

        billing_val = parse_tres(j.get("alloc_tres", "")).get("billing")
        if billing_val and elapsed_s:
            try:
                total_sus += int(billing_val) * elapsed_s / 3600.0
            except ValueError:
                pass

        if ct:  total_cpu_alloc += ct
        if tc:  total_cpu_used  += tc
        if total_req_mb: total_mem_req  += total_req_mb
        if rss: total_mem_used  += rss
        if gpu_n and elapsed_s and state == "COMPLETED":
            gh = gpu_n * elapsed_s / 3600.0
            total_gpu_hrs_alloc += gh
            gpu_job_count += 1
            if timelimit_s and elapsed_s < timelimit_s:
                total_gpu_hrs_wasted += gpu_n * (timelimit_s - elapsed_s) / 3600.0

    total = sum(counts.values())
    print(f"\n  Jobs in window: {bold(str(total))}")
    print()

    for state, count in sorted(counts.items(), key=lambda x: -x[1]):
        sc = green if state == "COMPLETED" else (red if state in ("FAILED","TIMEOUT","OUT_OF_MEMORY") else yellow)
        pct = count / total * 100 if total else 0
        bar = "█" * int(pct / 3)
        print(f"  {sc(f'{state:<22}')} {count:>5}   {sc(bar)}  {pct:.1f}%")

    if total_sus > 0:
        print(f"\n  {bold('SUs charged (this window):')}  {bold(f'{total_sus:.2f}')}")

    print(f"\n  {bold('Resource waste (all completed jobs):')}")

    cpu_eff_agg = (total_cpu_used  / total_cpu_alloc * 100) if total_cpu_alloc > 0 else None
    mem_eff_agg = (total_mem_used  / total_mem_req   * 100) if total_mem_req   > 0 else None

    wasted_cpu_h  = sec_to_hours(total_cpu_alloc - total_cpu_used) if total_cpu_alloc and total_cpu_used else None
    wasted_mem_mb = total_mem_req - total_mem_used if total_mem_req and total_mem_used else None

    print(f"  CPU util:  {eff_color(cpu_eff_agg)}  {eff_bar(cpu_eff_agg)}"
          f"  {dim(f'wasted ≈ {wasted_cpu_h:.0f} CPU·hrs' if wasted_cpu_h else 'N/A')}")
    print(f"  Mem util:  {eff_color(mem_eff_agg)}  {eff_bar(mem_eff_agg)}"
          f"  {dim(f'wasted ≈ {fmt_mb(wasted_mem_mb)}' if wasted_mem_mb else 'N/A')}")

    if total_gpu_hrs_alloc > 0:
        gpu_wall_util = ((total_gpu_hrs_alloc - total_gpu_hrs_wasted) / total_gpu_hrs_alloc * 100)
        print(f"  GPU wall util: {eff_color(gpu_wall_util)}  {eff_bar(gpu_wall_util)}"
              f"  {dim(f'{gpu_job_count} GPU jobs · {total_gpu_hrs_alloc:.1f} GPU·hrs allocated · {total_gpu_hrs_wasted:.1f} GPU·hrs wasted (wall-time idle)')}")
    else:
        print(f"  GPU  {dim('no GPU jobs found in this window')}")

    # ── aggregate tips ─────────────────────────────────────────────────────
    printed_tip = False
    if cpu_eff_agg is not None and cpu_eff_agg < 50:
        print(f"\n  {red('▲')} {bold('CPU pattern:')} Across your jobs, only {cpu_eff_agg:.0f}% of allocated CPU time was used.")
        print(f"     Reduce --cpus-per-task or --ntasks." + (f" Wasted ≈ {wasted_cpu_h:.0f} CPU·hrs." if wasted_cpu_h is not None else ""))
        printed_tip = True
    if mem_eff_agg is not None and mem_eff_agg < 50:
        print(f"\n  {yellow('▲')} {bold('Memory pattern:')} Average memory utilization is {mem_eff_agg:.0f}%. "
              f"Reduce --mem or --mem-per-cpu." + (f" Wasted ≈ {fmt_mb(wasted_mem_mb)}." if wasted_mem_mb else ""))
        printed_tip = True
    if total_gpu_hrs_alloc > 0 and total_gpu_hrs_wasted / total_gpu_hrs_alloc > 0.25:
        pct_wasted = total_gpu_hrs_wasted / total_gpu_hrs_alloc * 100
        print(f"\n  {red('▲')} {bold('GPU time pattern:')} {pct_wasted:.0f}% of allocated GPU·hrs were wasted "
              f"({total_gpu_hrs_wasted:.1f} of {total_gpu_hrs_alloc:.1f} GPU·hrs). "
              f"Shorten --time limits on GPU jobs.")
        printed_tip = True
    if not printed_tip:
        print(f"\n  {green('✓')} No major waste patterns detected.")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Slurm per-job resource waste analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-u", "--user",     default=None,
                    help="Username to report on (default: you)")
    ap.add_argument("-d", "--days",     type=int, default=7,
                    help="Days of history (default: 7)")
    ap.add_argument("-n", "--num-jobs", type=int, default=25,
                    help="Max completed jobs to show (default: 25)")
    ap.add_argument("--gpu-only",       action="store_true",
                    help="Show only GPU jobs")
    ap.add_argument("--tips-only",      action="store_true",
                    help="Show only actionable tips, skip detailed rows")
    ap.add_argument("--no-queue",       action="store_true",
                    help="Skip the queue snapshot line")
    ap.add_argument("--no-color",       action="store_true",
                    help="Disable ANSI colors (for logging/piping)")
    args = ap.parse_args()

    global _color_enabled
    if args.no_color or not sys.stdout.isatty():
        _color_enabled = False

    user = args.user or whoami()

    print(f"\n{bold(blue('Slurm Waste Analyzer'))}  |  user={bold(user)}"
          f"  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not args.no_queue:
        print()
        show_queue_snapshot(user)

    jobs = show_history(
        user,
        days=args.days,
        max_jobs=args.num_jobs,
        gpu_only=args.gpu_only,
        tips_only=args.tips_only,
    )

    show_summary(user, days=args.days, jobs=jobs)


if __name__ == "__main__":
    main()
