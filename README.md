# slurm_monitor

**Per-job resource waste analyzer for Slurm HPC clusters.**

Tells you exactly how much CPU time, memory, wall time, and GPU·hours you
requested but never used — so you can right-size your next job and get it
scheduled faster.

```
  12345678  my_train_job   COMPLETED  partition=gpu  nodes=gpu-node-01  exit=0:0
  ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Wall time:         requested=    48:00:00   used=    11:23:07   wasted=    36:36:53   time used= 23.7%  ████░░░░░░░░░░░░░░░░  ▲ high waste
  CPU time:          allocated=    91:04:56   used=    10:51:22   wasted=    80:13:34   cpu util= 11.9%  ██░░░░░░░░░░░░░░░░░░  ▲ high waste  (8 cores, ~7.1 idle)
  Memory (RAM):      requested=         64.0 GB   peak=      18.3 GB   avg=      14.2 GB   wasted=      45.7 GB   mem util= 28.6%  █████░░░░░░░░░░░░░░░
  GPU:               allocated=    2 GPU(s)   GPU·hrs allocated=  22.77 GPU·hr   wasted≈73.23 GPU·hr  (wall-time waste × 2 GPU)
  SUs charged:              91.08  (billing=8 × 11.38 hrs)
  ────────────────────────────────────────────────────────────
  ▲ wall time: Used 24% of --time limit (36:36:53 wasted). Set --time to ~13:47:41 (+20% buffer).
  ▲ CPU cores: Only 12% CPU efficiency — ~7.1 of 8 cores idle. Try --cpus-per-task=2.
  ▲ memory:    Peak RAM was 29% of request (45.7 GB unused). Try --mem=22.9 GB (+25% buffer).
  ▲ GPU time:  2 GPU(s) allocated but 73.2 GPU·hrs went unused. Reduce --time to recover them sooner.
```

---

## Features

| Feature | Description |
|---------|-------------|
| Wall-time waste | Requested vs. used time; wasted hours and efficiency % |
| CPU waste | Allocated CPU·hrs vs. consumed; equivalent number of idle cores |
| Memory waste | Requested vs. peak RSS; exact GB wasted per job |
| GPU·hrs waste | GPUs allocated × wall-time wasted — reads directly from `sacct`, no extra tools needed |
| Per-job tips | Concrete `--time`, `--cpus-per-task`, `--mem` values to try next run |
| Aggregate summary | Totals across all jobs with pattern-level tips when waste is systemic |
| GPU-only filter | Focus the report on GPU jobs only (`--gpu-only`) |
| Tips-only mode | Quick one-liner per job, no detailed rows (`--tips-only`) |
| Queue snapshot | One-line count of your running/pending jobs at the top |
| Zero dependencies | Pure Python stdlib + standard Slurm commands (`sacct`, `squeue`) |

---

## Requirements

- Python 3.6+
- Slurm `sacct` and `squeue` available in `PATH` (standard on any login node)
- No pip installs, no compiled code, no Prometheus, no NVML

---

## Quick start

```bash
git clone https://github.com/rccUser001/slurm_monitor.git
cd slurm_monitor
python3 slurm_monitor.py
```

Or make it executable and put it on your PATH:

```bash
chmod +x slurm_monitor.py
cp slurm_monitor.py ~/.local/bin/slurm_monitor
slurm_monitor
```

---

## Usage

```
python3 slurm_monitor.py [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-u USERNAME` | current user | Analyze jobs for a specific user |
| `-d DAYS` | `7` | Days of history to look back |
| `-n NUM` | `25` | Max completed jobs to show |
| `--gpu-only` | off | Show only jobs that requested GPU resources |
| `--tips-only` | off | Print only actionable tips, skip the detailed rows |
| `--no-queue` | off | Skip the live queue snapshot |
| `--no-color` | off | Plain text output (for logging or piping) |

---

## Examples

### Your jobs from the last 7 days
```bash
python3 slurm_monitor.py
```

### Look back 30 days
```bash
python3 slurm_monitor.py -d 30
```

### GPU jobs only
```bash
python3 slurm_monitor.py --gpu-only
```

### Quick tips summary — no detailed rows
```bash
python3 slurm_monitor.py --tips-only
```

Sample output:
```
  12345678  my_train_job   COMPLETED
    ▲ wall time: Used 24% of --time limit (36:36:53 wasted). Set --time to ~13:47:41 (+20% buffer).
    ▲ CPU cores: Only 12% CPU efficiency — ~7.1 of 8 cores idle. Try --cpus-per-task=2.
    ▲ memory:    Peak RAM was 29% of request (45.7 GB unused). Try --mem=22.9 GB (+25% buffer).
    ▲ GPU time:  2 GPU(s) allocated but 73.2 GPU·hrs went unused. Reduce --time to recover them.

  12345699  data_preproc   COMPLETED
    ▲ memory:    Peak RAM was 12% of request (112 GB unused). Try --mem=16.0 GB (+25% buffer).
```

### Save a plain-text report to a file
```bash
python3 slurm_monitor.py -d 30 --no-color > waste_report_$(date +%F).txt
```

### Check another user's jobs (requires Slurm permissions)
```bash
python3 slurm_monitor.py -u netid123 -d 14
```

---

## Understanding the output

### Per-job detail block

Each completed job prints resource rows (wall time, CPU, memory, GPU if applicable),
an SUs charged line, and — where waste is detected — a set of tips with concrete fix suggestions:

```
  Wall time:         requested=    48:00:00   used=    11:23:07   wasted=    36:36:53   time used= 23.7%
  CPU time:          allocated=    91:04:56   used=    10:51:22   wasted=    80:13:34   cpu util= 11.9%  (8 cores, ~7.1 idle)
  Memory (RAM):      requested=         64.0 GB   peak=      18.3 GB   avg=      14.2 GB   wasted=      45.7 GB   mem util= 28.6%
  GPU:               allocated=    2 GPU(s)   GPU·hrs allocated=  22.77 GPU·hr   wasted≈73.23 GPU·hr  (wall-time waste × 2 GPU)
  SUs charged:              91.08  (billing=8 × 11.38 hrs)
```

| Row | What it measures |
|-----|-----------------|
| **Wall time** | `--time` requested vs. how long the job actually ran |
| **CPU time** | `--cpus-per-task × elapsed` (allocated) vs. actual CPU seconds consumed |
| **Memory** | `--mem` (or `--mem-per-cpu`) vs. peak RSS recorded by Slurm |
| **GPU** | GPU count × elapsed = GPU·hrs billed; waste = GPU count × wasted wall time |

> **Note on GPU metrics:** GPU·hrs waste is derived from wall-time
> inefficiency (`sacct` data only). Per-GPU core utilization % (e.g. SM
> utilization) requires on-node instrumentation and is out of scope here.

### Efficiency bar colors

```
  ■ green  ≥75%  — good, minimal waste
  ■ yellow ≥40%  — moderate waste, worth adjusting
  ■ red    <40%  — high waste, strongly consider right-sizing
```

### Aggregate waste summary

After the per-job section, a summary shows totals across all jobs in the
window and prints pattern-level tips when waste exceeds 25–50%:

```
  CPU util:       11.9%  ██░░░░░░░░░░░░░░░░░░  wasted ≈ 320 CPU·hrs
  Mem util:       28.6%  █████░░░░░░░░░░░░░░░  wasted ≈ 183 GB
  GPU wall util:  23.7%  ████░░░░░░░░░░░░░░░░  4 GPU jobs · 91 GPU·hrs allocated · 69 GPU·hrs wasted (wall-time idle)

  ▲ CPU pattern: Across your jobs, only 12% of allocated CPU time was used.
     Reduce --cpus-per-task or --ntasks.

  ▲ Memory pattern: Average memory utilization is 29%. Reduce --mem or --mem-per-cpu.

  ▲ GPU time pattern: 76% of allocated GPU·hrs were wasted (69 of 91 GPU·hrs). Shorten --time limits on GPU jobs.
```

---

## Why right-sizing matters

When you over-request resources, Slurm has fewer available slots to fit other
jobs — including your own queued ones. Accurate requests:

- **Start sooner** — the scheduler finds a fit faster in the backfill window
- **Free GPUs earlier** — other jobs (and your own queue) can use them
- **Improve fairshare** — your account's usage reflects actual consumption,
  keeping your priority score healthy

---

## Common patterns and fixes

### Wall-time limit too generous
The most impactful fix for GPU jobs. Use the per-job `--time` tip printed by
the tool (actual elapsed × 1.2):
```bash
#SBATCH --time=14:00:00    # was 48:00:00; actual run was ~11.4h
```

### CPU efficiency consistently low
Your code uses fewer threads than you requested. Check the actual thread count
your program spawns and match `--cpus-per-task` to it:
```bash
#SBATCH --cpus-per-task=4  # was 16; job only used ~3.8 cores on average
```

### Memory efficiency low
Peak RSS was well below your request. Use the `--mem` tip (peak × 1.25):
```bash
#SBATCH --mem=23G           # was 128G; peak was 18.3 GB
```

### Job ended with OUT_OF_MEMORY
The opposite problem — your `--mem` was too low and Slurm killed the job.
Increase it and resubmit:
```bash
#SBATCH --mem=64G           # up from 32G that triggered OOM
```

### GPU·hrs wasted across many jobs
A generous `--time` on GPU jobs is the most common cause. Tighten the wall
limit to match your typical runtime:
```bash
#SBATCH --time=14:00:00     # down from 48:00:00; saves ~68 GPU·hrs per run
```

---

## Status and contributions

This tool is actively developed and may contain inaccuracies or missing edge
cases. If you notice incorrect output, unexpected behavior, or a metric that
doesn't match what you see in `sacct` or `rcchelp`, please open an issue or
submit a pull request — contributions are very welcome.

When reporting a bug, including the job ID and the output of:
```bash
sacct -j <jobid> --format=JobID,Elapsed,CPUTimeRAW,TotalCPU,ReqMem,MaxRSS,AllocTRES%120
```
makes it much easier to reproduce and fix.

---

## License

MIT
