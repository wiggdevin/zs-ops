#!/usr/bin/env python3
"""telemetry_digest.py — condense the last N hours of machine and agent telemetry for a Hermes review.

Added 2026-10-29 with telemetry_sample.py. The 8-hourly Hermes "telemetry-review" cron job
runs this; its stdout becomes the agent's prompt input, so it stays short (a few thousand
tokens) and dense: numbers and counts, a few example lines, no raw dumps.

Sources (each optional; a missing file is skipped):
  ~/Library/Logs/zs-telemetry/machine-*.jsonl   telemetry_sample.py, every 5 min
  ~/Library/Logs/zs-memwatch.jsonl              memory totals, every minute
  ~/Library/Logs/zs-sweep.jsonl, zs-memguard.jsonl, zs-headless-reaper.jsonl, zs-watchdog.jsonl
  ~/.claude/logs/subagent-router.log, done-check.log
  ~/.hermes/logs/agent.log                      Hermes model latency, approval timeouts, tool errors

Usage: telemetry_digest.py [HOURS]   (default 8). Read-only. Runs on /usr/bin/python3 (3.9).
"""
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
LOGS = HOME / "Library/Logs"
TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def epoch(text):
    """ISO timestamp (naive = local, Z, or +hhmm) -> epoch seconds, or None."""
    m = TS.search(text or "")
    if not m:
        return None
    s = m.group(0).replace(",", ".").replace(" ", "T").replace("Z", "+0000")
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)
    for f in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, f).timestamp()
        except ValueError:
            continue
    return None


def tail_lines(path, since, max_bytes=8_000_000):
    """Lines of path written at or after `since` (by their own timestamp); reads at most the last max_bytes."""
    if not path.exists():
        return []
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - max_bytes))
        data = fh.read().decode("utf-8", "replace").splitlines()
    out = []
    for line in data:
        t = epoch(line[:80]) or epoch(line[-60:])
        if t is not None and t >= since:
            out.append(line)
    return out


def jsonl(lines):
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def stats(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return "n/a"
    vals.sort()
    return "min %s · med %s · p90 %s · max %s" % (vals[0], round(statistics.median(vals), 2),
                                                 vals[int(len(vals) * 0.9)], vals[-1])


def machine(since):
    recs = []
    for f in sorted((LOGS / "zs-telemetry").glob("machine-*.jsonl"))[-3:]:
        recs += [r for r in jsonl(f.read_text().splitlines()) if (epoch(r.get("ts")) or 0) >= since]
    if not recs:
        return ["## machine: no samples in window"]
    cpu, mem, sess = {}, {}, {}
    for r in recs:
        for fam, v in r.get("top_cpu", []):
            cpu[fam] = cpu.get(fam, 0) + v
        for fam, v in r.get("top_mem_gb", []):
            mem[fam] = max(mem.get(fam, 0), v)
        for k, v in r.get("sessions", {}).items():
            sess[k] = max(sess.get(k, 0), v)
    n = len(recs)
    busy = [r for r in recs if r.get("sessions")]
    out = ["## machine (%d samples, %d with an agent session open)" % (n, len(busy)),
           "- load1: " + stats([r["load"][0] for r in recs]),
           "- cpu % total (100 = one core): " + stats([r.get("cpu_pct_total") for r in recs]),
           "- spawn ms: " + stats([r.get("spawn_ms") for r in recs]),
           "- compressor GB: " + stats([r.get("compressed_gb") for r in recs]),
           "- swap GB: " + stats([r.get("swap_gb") for r in recs]),
           "- pressure >1 (warn/critical) samples: %d" % sum(1 for r in recs if (r.get("pressure") or 0) > 1),
           "- dev orphans: " + stats([r.get("dev_orphans") for r in recs]),
           "- procs: " + stats([r.get("procs") for r in recs]),
           "- disk free GB: first %s, last %s" % (recs[0].get("disk_free_gb"), recs[-1].get("disk_free_gb")),
           "- max parallel sessions: " + (", ".join("%s %d" % kv for kv in sorted(sess.items())) or "none"),
           "- avg CPU % by family: " + ", ".join("%s %d" % (f, v / n) for f, v in
                                                  sorted(cpu.items(), key=lambda x: -x[1])[:10]),
           "- peak memory GB by family: " + ", ".join("%s %s" % (f, v) for f, v in
                                                      sorted(mem.items(), key=lambda x: -x[1])[:10])]
    return out


def events(name, path, since, show=3):
    lines = tail_lines(path, since)
    out = ["- %s: %d lines" % (name, len(lines))]
    out += ["  - " + l[:220] for l in lines[-show:]] if show else []
    return out


def sweep_section(since):
    recs = jsonl(tail_lines(LOGS / "zs-sweep.jsonl", since))
    acts = {}
    for r in recs:
        acts[r.get("action")] = acts.get(r.get("action"), 0) + 1
    out = ["## guards", "- sweep actions: " + (", ".join("%s %d" % kv for kv in sorted(acts.items())) or "none")]
    out += ["  - %s pid %s %s: %s" % (r.get("action"), r.get("pid"), r.get("rule", r.get("reason", "")),
                                      str(r.get("cmd"))[:140])
            for r in recs if r.get("action") not in ("report", None)][-6:]
    out += events("memguard stops", LOGS / "zs-memguard.jsonl", since)
    out += events("headless-reaper", LOGS / "zs-headless-reaper.jsonl", since)
    wd = jsonl(tail_lines(LOGS / "zs-watchdog.jsonl", since))
    bad = [r for r in wd if r.get("status") not in ("healthy", "ok", None)]
    out.append("- watchdog: %d lines, %d not healthy" % (len(wd), len(bad)))
    out += ["  - %s %s %s" % (r.get("component"), r.get("status"), str(r.get("action"))[:120]) for r in bad[-4:]]
    mw = jsonl(tail_lines(LOGS / "zs-memwatch.jsonl", since))
    out.append("- memwatch: %d samples, %d triggered, available MB %s" % (
        len(mw), sum(1 for r in mw if r.get("triggered")), stats([r.get("available_mb") for r in mw])))
    return out


def claude_section(since):
    router = jsonl(tail_lines(HOME / ".claude/logs/subagent-router.log", since))
    picks = {}
    for r in router:
        k = "skip:" + r["skip"] if r.get("skip") else "%s:%s" % (r.get("surface", "claude"), r.get("tier"))
        picks[k] = picks.get(k, 0) + 1
    done = jsonl(tail_lines(HOME / ".claude/logs/done-check.log", since))
    return ["## claude code",
            "- subagent router: %d decisions: %s" % (len(router), ", ".join(
                "%s %d" % kv for kv in sorted(picks.items(), key=lambda x: -x[1])[:8])),
            "- done-check: %d checks, %d blocked, seconds %s" % (
                len(done), sum(1 for r in done if r.get("blocked")), stats([r.get("s") for r in done]))]


def hermes_section(since):
    lines = tail_lines(HOME / ".hermes/logs/agent.log", since, max_bytes=20_000_000)
    lat, ins, timeouts, errors, comp, turns = [], [], 0, {}, [], []
    for l in lines:
        m = re.search(r"API call #\d+: model=(\S+).* in=(\d+) out=\d+.*latency=([\d.]+)s", l)
        if m:
            lat.append(float(m.group(3)))
            ins.append(int(m.group(2)))
        if "timed out without user response" in l:
            timeouts += 1
        m = re.search(r"Tool (\w+) returned error \(([\d.]+)s\)", l)
        if m:
            errors[m.group(1)] = errors.get(m.group(1), 0) + 1
        m = re.search(r'"aux_call_duration_ms":(\d+)', l)
        if m:
            comp.append(int(m.group(1)) / 1000)
        m = re.search(r"tui turn finished: .*duration=([\d.]+)s", l)
        if m:
            turns.append(float(m.group(1)))
    return ["## hermes",
            "- model calls: %d · latency s: %s" % (len(lat), stats(lat)),
            "- input tokens per call: " + stats(ins),
            "- turn duration s: " + stats(turns),
            "- compressions: %d · seconds %s" % (len(comp), stats(comp)),
            "- approval timeouts: %d" % timeouts,
            "- tool errors: " + (", ".join("%s %d" % kv for kv in sorted(errors.items(), key=lambda x: -x[1]))
                                 or "none")]


def main(argv):
    hours = float(argv[0]) if argv else 8.0
    since = time.time() - hours * 3600
    out = ["# telemetry digest: last %g h, ending %s" % (hours, time.strftime("%Y-%m-%d %H:%M %Z")), ""]
    for section in (machine, sweep_section, claude_section, hermes_section):
        try:
            out += section(since)
        except Exception as e:  # one broken source must not hide the others
            out.append("## %s: error %s" % (section.__name__, e))
        out.append("")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
