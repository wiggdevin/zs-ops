#!/usr/bin/env python3
"""memwatch.py — snapshot per-process memory when the machine is heading for a hang.

Written after the 2026-07-30 hard reset. Claude Desktop's own telemetry caught the
run-up (TOTAL 6356MB/75 children at 07:14 to 9779MB/192 children at 07:19, then
3.5 minutes of silence and a power-button reset) but there was no per-process
record, so the offenders had to be inferred after the fact.

Two triggers, both drawn from that incident:

  low memory    available RAM below ZS_MEMWATCH_THRESHOLD_MB. The hang hit at
                1883MB free of 48GB; the 4GB default fires during the run-up
                rather than at the cliff.
  process count total processes above ZS_MEMWATCH_PROC_MAX. The primary leak was
                a handle/process leak (claude-science spawning run_server.py
                roughly once a minute, never reaping), which shows up in the
                count long before it shows up in RSS.

Every run appends one compact line to the JSONL trend log. Only a triggered run
writes a full snapshot, and snapshots are rate-limited and capped in count so
this can never become the next uncapped log.

Runs on /usr/bin/python3 (system python, 3.9) so it has no mise/venv dependency.

Tuning (env):
  ZS_MEMWATCH_THRESHOLD_MB     available-RAM trigger, MB          (default 4096)
  ZS_MEMWATCH_PROC_MAX         process-count trigger              (default 1200)
  ZS_MEMWATCH_MIN_INTERVAL     min seconds between snapshots      (default 300)
  ZS_MEMWATCH_KEEP             snapshots retained                 (default 200)
  ZS_MEMWATCH_TREND_CAP        trend-log size cap, bytes          (default 5MB)
  ZS_MEMWATCH_FORCE            set to 1 to snapshot unconditionally

Audit trail: ~/Library/Logs/zs-memwatch.jsonl
Snapshots:   ~/Library/Logs/zs-memwatch/
"""

import json
import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path

LOG_DIR = Path.home() / "Library/Logs/zs-memwatch"
TREND_LOG = Path.home() / "Library/Logs/zs-memwatch.jsonl"

THRESHOLD_MB = int(os.environ.get("ZS_MEMWATCH_THRESHOLD_MB", 4096))
PROC_MAX = int(os.environ.get("ZS_MEMWATCH_PROC_MAX", 1200))
MIN_INTERVAL = int(os.environ.get("ZS_MEMWATCH_MIN_INTERVAL", 300))
KEEP = int(os.environ.get("ZS_MEMWATCH_KEEP", 200))
TREND_CAP_BYTES = int(os.environ.get("ZS_MEMWATCH_TREND_CAP", 5 * 1024 * 1024))
FORCE = os.environ.get("ZS_MEMWATCH_FORCE") == "1"

# Process names worth calling out by name in the snapshot summary. Everything is
# captured regardless; this only decides what gets a dedicated rollup.
WATCHED = ("claude", "mcp", "coworkd", "run_server", "node", "python")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


def memory_state():
    """Available MB and compressor MB, from vm_stat."""
    out = run(["/usr/bin/vm_stat"])
    page_size = 4096
    pages = {}
    for line in out.splitlines():
        if "page size of" in line:
            page_size = int(line.split("page size of")[1].split("bytes")[0].strip())
        elif ":" in line:
            key, _, val = line.partition(":")
            val = val.strip().rstrip(".")
            if val.isdigit():
                pages[key.strip()] = int(val)

    def mb(*keys):
        return sum(pages.get(k, 0) for k in keys) * page_size // (1024 * 1024)

    # macOS "available" is roughly free + inactive + speculative + purgeable:
    # pages the kernel can hand out without swapping.
    return {
        "available_mb": mb(
            "Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"
        ),
        "compressed_mb": mb("Pages occupied by compressor"),
        "wired_mb": mb("Pages wired down"),
    }


def processes():
    """[(rss_kb, pid, ppid, command)] for every process, largest first."""
    out = run(["/bin/ps", "-Ao", "rss=,pid=,ppid=,command="])
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    rows.sort(reverse=True)
    return rows


def short_name(command):
    """argv[0] basename — the thing you'd group a leak by."""
    argv0 = command.split(None, 1)[0]
    return argv0.rsplit("/", 1)[-1] or argv0


def build_snapshot(mem, procs, reasons):
    by_name_rss = defaultdict(int)
    by_name_count = defaultdict(int)
    for rss, _pid, _ppid, command in procs:
        name = short_name(command)
        by_name_rss[name] += rss
        by_name_count[name] += 1

    watched = {
        name: {"count": by_name_count[name], "rss_mb": by_name_rss[name] // 1024}
        for name in by_name_count
        if any(w in name.lower() for w in WATCHED)
    }

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reasons": reasons,
        "memory": mem,
        "process_count": len(procs),
        "thresholds": {"available_mb": THRESHOLD_MB, "process_count": PROC_MAX},
        # Full command line kept: the 2026-07-30 diagnosis turned on spotting
        # which parent spawned the duplicates, which argv0 alone would have lost.
        "top_by_rss": [
            {"rss_mb": rss // 1024, "pid": pid, "ppid": ppid, "command": command[:300]}
            for rss, pid, ppid, command in procs[:40]
        ],
        "by_name": sorted(
            (
                {"name": n, "count": by_name_count[n], "rss_mb": by_name_rss[n] // 1024}
                for n in by_name_count
            ),
            key=lambda r: (-r["count"], -r["rss_mb"]),
        )[:40],
        "watched": watched,
    }


def prune():
    snaps = sorted(LOG_DIR.glob("snapshot-*.json"), reverse=True)
    for stale in snaps[KEEP:]:
        stale.unlink(missing_ok=True)


def cap_trend_log():
    """Keep the trend log bounded. It is not a launchd redirect, so rotate-logs.py
    does not cover it, and at a line per minute it would otherwise grow forever."""
    if not TREND_LOG.exists() or TREND_LOG.stat().st_size <= TREND_CAP_BYTES:
        return
    lines = TREND_LOG.read_text().splitlines(keepends=True)
    TREND_LOG.write_text("".join(lines[len(lines) // 2 :]))


def last_snapshot_age():
    snaps = sorted(LOG_DIR.glob("snapshot-*.json"), reverse=True)
    if not snaps:
        return None
    return time.time() - snaps[0].stat().st_mtime


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    mem = memory_state()
    procs = processes()

    reasons = []
    if mem["available_mb"] < THRESHOLD_MB:
        reasons.append("low_memory")
    if len(procs) > PROC_MAX:
        reasons.append("process_count")
    if FORCE:
        reasons.append("forced")

    trend = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "available_mb": mem["available_mb"],
        "compressed_mb": mem["compressed_mb"],
        "process_count": len(procs),
        "triggered": bool(reasons),
    }

    wrote = None
    if reasons:
        age = last_snapshot_age()
        # Rate limit, but never let it suppress a forced run.
        if FORCE or age is None or age >= MIN_INTERVAL:
            path = LOG_DIR / time.strftime("snapshot-%Y%m%d-%H%M%S.json")
            path.write_text(json.dumps(build_snapshot(mem, procs, reasons), indent=2))
            prune()
            wrote = str(path)
        else:
            trend["suppressed"] = round(MIN_INTERVAL - age)

    if wrote:
        trend["snapshot"] = wrote

    with TREND_LOG.open("a") as fh:
        fh.write(json.dumps(trend) + "\n")
    cap_trend_log()

    print(json.dumps(trend))


if __name__ == "__main__":
    main()
