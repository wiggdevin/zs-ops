#!/usr/bin/env python3
"""telemetry_sample.py — one dense machine sample every 5 minutes, for the 8-hourly Hermes review.

Added 2026-10-29. Devin wants telemetry while coding, collected for a couple of weeks, so a
Hermes job can find fixes, skills, hooks and restructuring from real data. memwatch.py
already records memory totals every minute; this adds what it lacks: CPU, load, swap,
process-spawn latency (the 2026-10-13 headless-browser load made every spawn ~20x slower),
running agent sessions by tool, and the heaviest process families.

One JSON line per run to ~/Library/Logs/zs-telemetry/machine-YYYY-MM-DD.jsonl. Files older
than KEEP_DAYS are deleted. Two `ps` reads 2 s apart give CPU per family; nothing is signalled.
Runs from launchd: ~/Library/LaunchAgents/com.zs.telemetry-sample.plist (every 300 s).
Off switch: ZS_TELEMETRY=0. Runs on /usr/bin/python3 (3.9), no third-party modules.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memguard as mg  # noqa: E402
import sweep as sw  # noqa: E402

DIR = Path.home() / "Library/Logs/zs-telemetry"
KEEP_DAYS = 30
WINDOW_S = 2.0
AGENTS = {  # family -> regex over the full command line of a session's main process
    "claude": r"(^|/)claude(\s|$)",
    "codex": r"(^|/)codex(\s|$)|@openai/codex",
    "hermes": r"hermes-official/ui-tui|-m tui_gateway",
    "agy": r"(^|/)agy(\s|$)",
    "kimi": r"(^|/)kimi-k3(\s|$)",
    "cursor-agent": r"(^|/)cursor-agent(\s|$)",
}


def family(cmd):
    """A short stable label: app name for apps, script or package name for interpreters, else the exe."""
    m = re.match(r"^/\S*?/([^/]+)\.app/", cmd)
    if m:
        return m.group(1)
    exe = sw.exe_name(cmd)
    if re.match(r"^(node[\d.]*|bun|python[\d.]*|uv|uvx|npx|deno)$", exe):
        toks = cmd.split()[1:8]
        for i, tok in enumerate(toks):
            if tok in ("-c", "-e", "--eval"):
                return "%s:inline" % exe
            if tok == "-m" and i + 1 < len(toks):
                return "%s:%s" % (exe, toks[i + 1][:40])
            if tok.startswith("-"):
                continue
            pkg = re.search(r"node_modules/(\.bin/)?((@[^/]+/)?[^/\s]+)", tok)
            if pkg:
                return "%s:%s" % (exe, pkg.group(2))
            return "%s:%s" % (exe, os.path.basename(tok.rstrip("/"))[:40])
    return exe[:40]


def spawn_ms(n=10):
    t = time.monotonic()
    for _ in range(n):
        subprocess.run(["/usr/bin/true"])
    return round((time.monotonic() - t) * 1000 / n, 1)


def swap_gb():
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"used = ([\d.]+)M", out)
        return round(float(m.group(1)) / 1024, 2) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def sample():
    before = sw.read_table()
    time.sleep(WINDOW_S)
    after = sw.read_table()
    cpu, mem, count = {}, {}, {}
    for pid, v in after.items():
        fam = family(v["cmd"])
        b = before.get(pid)
        if b and b["cmd"] == v["cmd"]:
            cpu[fam] = cpu.get(fam, 0.0) + max(0.0, v["cpu_s"] - b["cpu_s"]) / WINDOW_S * 100
        f = mg.facts(pid)
        if f:
            mem[fam] = mem.get(fam, 0) + f[0]
        count[fam] = count.get(fam, 0) + 1
    sessions = {name: sum(1 for v in after.values() if re.search(rx, v["cmd"]) and v["tty"] not in ("??", ""))
                for name, rx in AGENTS.items()}
    orphans = sum(1 for v in after.values() if v["ppid"] == 1 and sw.DEV_FAMILY.match(sw.exe_name(v["cmd"])))
    comp = mg.compressor_bytes()
    st = os.statvfs("/")
    top = lambda d, k, scale: [[f, round(v / scale, 1)] for f, v in sorted(d.items(), key=lambda x: -x[1])[:k]]
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cpu_pct_total": round(sum(cpu.values())),
        "procs": len(after),
        "dev_orphans": orphans,
        "compressed_gb": round(comp / mg.GB, 2) if comp else None,
        "pressure": mg.sysctl_int("kern.memorystatus_vm_pressure_level"),
        "swap_gb": swap_gb(),
        "disk_free_gb": round(st.f_bavail * st.f_frsize / mg.GB, 1),
        "spawn_ms": spawn_ms(),
        "sessions": {k: v for k, v in sessions.items() if v},
        "top_cpu": top(cpu, 8, 1),
        "top_mem_gb": top(mem, 8, mg.GB),
        "top_count": sorted(count.items(), key=lambda x: -x[1])[:6],
    }


def prune(now):
    for f in DIR.glob("machine-*.jsonl"):
        if now - f.stat().st_mtime > KEEP_DAYS * 86400:
            f.unlink()


def main():
    if os.environ.get("ZS_TELEMETRY", "1") == "0":
        return 0
    DIR.mkdir(parents=True, exist_ok=True)
    rec = sample()
    with (DIR / time.strftime("machine-%Y-%m-%d.jsonl")).open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    prune(time.time())
    if "--print" in sys.argv:
        print(json.dumps(rec, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
