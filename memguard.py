#!/usr/bin/env python3
"""memguard.py — stop runaway test runners before the machine runs out of memory.

Written after the 2026-10-21 kernel panic. Seven agents ran the memo-reader vision
suite (birefnet, 12-30 GB per pytest process) at once; with llama-server they held
about 142 GB on this 48 GB Mac. The memory compressor went from 1.3 GB to 25 GB in
five minutes (03:00-03:05), jetsam killed system daemons, watchdogd stalled and the
kernel panicked between 03:10 and 03:16. memwatch saw it all and did nothing: its
"available" figure counts inactive pages and never fell below 6.6 GB.

Only processes whose command line matches KILLABLE (test runners) are ever stopped.
Everything else is reported, never touched. Two rules:

  per-process cap  a killable process above PROC_GB is stopped at once, whatever
                   the pressure. No test run on this machine needs that much.
  pressure         when the compressor holds more than COMPRESSED_GB, or the kernel
                   reports critical pressure, killable processes are stopped largest
                   first until the killable total is at or below KEEP_GB. KEEP_GB
                   leaves room for one legitimate vision run.

Memory per process is the footprint `top` reports (resident plus compressed), not
RSS: a process whose pages were compressed looks small in RSS while it fills the
compressor.

Stopping is SIGTERM, then SIGKILL after GRACE_S. Each stop is one JSONL line in
~/Library/Logs/zs-memguard.jsonl and a desktop notification.

Called every 60 s from memwatch.py and every sweep from watchdog.sh (check_memguard).
Runs on /usr/bin/python3 (3.9), no third-party modules.

Tuning (env):
  ZS_MEMGUARD_KILLABLE       regex on the full command line   (default: pytest|vitest|jest)
  ZS_MEMGUARD_PROC_GB        per-process cap, GB              (default 32)
  ZS_MEMGUARD_COMPRESSED_GB  compressor trigger, GB           (default 16)
  ZS_MEMGUARD_KEEP_GB        killable total kept under pressure (default 16)
  ZS_MEMGUARD_GRACE_S        SIGTERM to SIGKILL, seconds       (default 5)
  ZS_MEMGUARD_DRY_RUN        1 = decide and log, stop nothing
  ZS_MEMGUARD=0              turn the guard off

Exit 0 always when it ran; the JSON line on stdout says what it did.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG = Path.home() / "Library/Logs/zs-memguard.jsonl"
LOG_CAP_BYTES = 2 * 1024 * 1024


def _env_float(name, default):
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


def config():
    return {
        "killable": os.environ.get("ZS_MEMGUARD_KILLABLE", "").strip()
        or r"(^|[\s/])(pytest|py\.test|vitest|jest)(\s|$|\.m?js\b)|-m\s+pytest\b",
        "proc_gb": _env_float("ZS_MEMGUARD_PROC_GB", 32.0),
        "compressed_gb": _env_float("ZS_MEMGUARD_COMPRESSED_GB", 16.0),
        "keep_gb": _env_float("ZS_MEMGUARD_KEEP_GB", 16.0),
        "grace_s": _env_float("ZS_MEMGUARD_GRACE_S", 5.0),
        "dry_run": os.environ.get("ZS_MEMGUARD_DRY_RUN") == "1",
    }


def run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


_UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_mem(text):
    """'10G', '823M', '512K+', '1.5G-' -> bytes; None when unreadable."""
    m = re.match(r"^\s*([\d.]+)([BKMGT])?[+-]?\s*$", text)
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2) or "B"])


def footprints(top_out):
    """{pid: bytes} from `top -l 1 -stats pid,mem` output."""
    out = {}
    for line in top_out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            b = parse_mem(parts[1])
            if b is not None:
                out[int(parts[0])] = b
    return out


def commands(ps_out):
    """{pid: full command line} from `ps -Ao pid=,command=`."""
    out = {}
    for line in ps_out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            out[int(pid)] = cmd.strip()
    return out


def compressed_bytes(vm_stat_out):
    page, pages = 4096, 0
    for line in vm_stat_out.splitlines():
        if "page size of" in line:
            page = int(line.split("page size of")[1].split("bytes")[0])
        elif line.startswith("Pages occupied by compressor"):
            pages = int(line.split(":")[1].strip().rstrip("."))
    return pages * page


def pressure_level():
    """1 normal, 2 warn, 4 critical (kern.memorystatus_vm_pressure_level); 0 unknown."""
    try:
        return int(run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0


def decide(procs, compressed, level, cfg, me=os.getpid()):
    """procs: [(pid, bytes, command)]. Returns (victims [(pid, bytes, command, rule)], reason).

    Pure: no I/O, so the policy is testable without a machine under pressure.
    """
    pat = re.compile(cfg["killable"])
    gb = 1024 ** 3
    killable = sorted((p for p in procs if p[0] != me and pat.search(p[2])), key=lambda p: -p[1])
    capped = [p for p in killable if p[1] > cfg["proc_gb"] * gb]
    victims = [(*p, "proc_cap") for p in capped]
    under_pressure = compressed > cfg["compressed_gb"] * gb or level >= 4
    if under_pressure:
        rest = [p for p in killable if p not in capped]
        total = sum(p[1] for p in rest)
        for p in rest:  # largest first
            if total <= cfg["keep_gb"] * gb:
                break
            victims.append((*p, "pressure"))
            total -= p[1]
    reason = "pressure" if under_pressure else ("proc_cap" if victims else "")
    return victims, reason


def _alive(pid):
    """True while the process exists and has not exited; a zombie only waits for its parent to reap it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not run(["/bin/ps", "-o", "stat=", "-p", str(pid)]).strip().startswith("Z")


def stop(pid, grace_s):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _alive(pid):
            return "terminated"
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
        return "killed"
    except ProcessLookupError:
        return "terminated"


def notify(msg):
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        'display notification "%s" with title "zs-memguard"' % msg.replace('"', "'")],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def append_log(rec):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if LOG.stat().st_size > LOG_CAP_BYTES:
        lines = LOG.read_text().splitlines(keepends=True)
        LOG.write_text("".join(lines[len(lines) // 2:]))


def main():
    if os.environ.get("ZS_MEMGUARD") == "0":
        print(json.dumps({"memguard": "off"}))
        return 0
    cfg = config()
    gb = 1024 ** 3
    fp = footprints(run(["/usr/bin/top", "-l", "1", "-o", "mem", "-n", "40", "-stats", "pid,mem"]))
    cmds = commands(run(["/bin/ps", "-Ao", "pid=,command="]))
    procs = [(pid, b, cmds.get(pid, "")) for pid, b in fp.items()]
    compressed = compressed_bytes(run(["/usr/bin/vm_stat"]))
    level = pressure_level()
    victims, reason = decide(procs, compressed, level, cfg)

    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "compressed_gb": round(compressed / gb, 1),
        "pressure_level": level,
        "reason": reason,
        "dry_run": cfg["dry_run"],
        "top": [{"pid": p, "gb": round(b / gb, 1), "command": c[:120]}
                for p, b, c in sorted(procs, key=lambda x: -x[1])[:5]],
        "stopped": [],
    }
    for pid, b, cmd, rule in victims:
        result = "dry-run" if cfg["dry_run"] else stop(pid, cfg["grace_s"])
        rec["stopped"].append({"pid": pid, "gb": round(b / gb, 1), "rule": rule,
                               "result": result, "command": cmd[:300]})
    if victims:
        append_log(rec)
        names = ", ".join("%s (%.0f GB)" % (re.sub(r"\s.*", "", c.split("/")[-1]) or p, b / gb)
                          for p, b, c, _ in victims)
        notify("%s %d test process(es): %s" % ("Would stop" if cfg["dry_run"] else "Stopped",
                                                 len(victims), names))
    print(json.dumps({k: rec[k] for k in ("compressed_gb", "pressure_level", "reason", "stopped")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
