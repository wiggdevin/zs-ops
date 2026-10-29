#!/usr/bin/env python3
"""sweep.py — the hourly process sweep behind the Hermes "system-sweep" cron job.

Added 2026-10-29. Devin asked for an hourly job that keeps important work safe and
clears orphans and runaway processes so the machine stays fast. The rules live here,
not in the agent prompt: the Hermes agent only reads the report and may ask this
script to stop a "review" process, and this script re-checks every rule first.

Protected, never signalled (is_protected):
  - launchd jobs (a pid in `launchctl list`) and everything below them;
  - apps and system binaries (/Applications, *.app/, /System, /usr/libexec, /usr/sbin, /Library);
  - agent and session tools and everything below them: claude, codex, hermes, agy, kimi,
    cursor, t3, grok, ollama, docker/orbstack, zsvault, ssh, tmux, screen, mosh, iTerm;
  - any process with a terminal, this script and its ancestors.
  EGO Lite is an app, so it is always protected. Orphaned headless browsers are the
  headless-reaper's job, not this one.

Stopped without asking (auto), only when ALL hold: the parent is launchd (pid 1) but it is
not a launchd job, it is not protected, it is a dev-tool process (DEV_FAMILY), and either
  runaway  CPU at or above RUNAWAY_CPU % over the sample window and older than RUNAWAY_AGE_S, or
  stale    older than STALE_AGE_S, under 1 % CPU, and lsof shows no socket at all, or
  shell    an orphaned Claude Code shell-snapshot shell older than SHELL_AGE_S.
Each one is stopped with its descendants through memguard.stop_all (SIGSTOP, SIGTERM,
SIGKILL after GRACE_S; the start time is re-read before each signal).

Review (the agent decides, through `sweep.py stop`): any other unprotected orphaned dev-tool
process older than an hour, and any unprotected process over RUNAWAY_CPU % or over
BIG_GB of memory. Watch (report only): protected processes over those limits.

Disk bloat is reported, never deleted.

Usage:
  sweep.py report            sample, apply the auto rules, print the report (the cron script)
  sweep.py stop PID IDENT    stop one review process; IDENT is the token from the report
  sweep.py --dry-run report  decide and report, signal nothing
Log: ~/Library/Logs/zs-sweep.jsonl. Off switch: ZS_SWEEP=0 (report only, no signals).
Runs on /usr/bin/python3 (3.9), no third-party modules.
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

LOG = Path.home() / "Library/Logs/zs-sweep.jsonl"
LOG_CAP_BYTES = 2 * 1024 * 1024
WINDOW_S = float(os.environ.get("ZS_SWEEP_WINDOW_S", "10"))
RUNAWAY_CPU = 80.0
RUNAWAY_AGE_S = 30 * 60
STALE_AGE_S = 6 * 3600
SHELL_AGE_S = 2 * 3600
REVIEW_AGE_S = 3600
BIG_GB = 4.0
GRACE_S = 5.0

SYSTEM_PATH = re.compile(r"^(/Applications/|/System/|/usr/libexec/|/usr/sbin/|/Library/|/sbin/|\S*\.app/)")
PROTECTED_NAME = re.compile(
    r"(^|/)(claude|codex|hermes|agy|antigravity|kimi(-k3)?|cursor(-agent)?|t3|grok(-subscription)?|ollama|"
    r"docker|com\.docker\S*|orbstack|orb|zsvault|ssh|sshd|ssh-agent|tmux|screen|mosh-server|login|launchd|"
    r"iTerm2?|cloudflared)$")
# Script names that mark a node/python process as an agent tool even though the exe is node/python.
PROTECTED_ARG = re.compile(r"(/|\b)(claude|codex|hermes|agy|t3code|zsvault)(\.m?js|\.py)?(\s|$)|"
                           r"-m (hermes\w*|tui_gateway)\b|"
                           r"hermes-official/|/\.hermes/|@anthropic-ai/claude-code|@openai/codex")
DEV_FAMILY = re.compile(
    r"^(node[\d.]*|bun|deno|python[\d.]*|uv|uvx|npx|npm|pnpm|yarn|tsx|esbuild|vite|next-server|turbo|"
    r"vitest|jest|playwright|ruby|go|cargo|zsh|bash|sh)$")
SNAPSHOT_SHELL = re.compile(r"^/bin/(zsh|bash) -c source \S*/\.claude/shell-snapshots/")
BLOAT_DIRS = ["~/Library/Caches", "~/.npm", "~/Library/pnpm", "~/.cache", "~/Library/Logs", "~/.hermes/logs",
              "~/.claude/projects", "~/Library/Group Containers/HUAQ24HBR6.dev.orbstack", "/private/tmp"]


def exe_name(cmd):
    return os.path.basename(cmd.split(" ", 1)[0]) if cmd else ""


def to_seconds(text):
    """ps etime ([[dd-]hh:]mm:ss) or cputime ([hh:]mm:ss.ss) -> seconds."""
    days = 0.0
    if "-" in text:
        d, text = text.split("-", 1)
        days = float(d)
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return days * 86400 + total


def parse_table(out):
    """{pid: dict} from `ps -o pid=,ppid=,etime=,time=,tty=,command=`."""
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            age, cpu = to_seconds(parts[2]), to_seconds(parts[3])
        except ValueError:
            continue
        procs[int(parts[0])] = {"ppid": int(parts[1]), "age": age, "cpu_s": cpu, "tty": parts[4],
                                "cmd": parts[5] if len(parts) > 5 else ""}
    return procs


def read_table():
    r = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,etime=,time=,tty=,command="],
                       capture_output=True, text=True, timeout=15)
    procs = parse_table(r.stdout)
    if r.returncode != 0 or not procs:
        raise mg.SampleError("ps exit %d, %d processes" % (r.returncode, len(procs)))
    return procs


def launchd_pids():
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return None  # unknown: the caller treats every pid-1 child as a launchd job
    return {int(l.split()[0]) for l in out.splitlines()[1:] if l.split() and l.split()[0].isdigit()}


def chain(procs, pid):
    """pid and its ancestors below launchd."""
    return mg.ancestors({p: (v["ppid"], v["cmd"]) for p, v in procs.items()}, pid)


def protected_reason(procs, pid, jobs, me_chain):
    if pid in me_chain:
        return "self"
    for p in chain(procs, pid):
        v = procs[p]
        if jobs is None and v["ppid"] == 1:
            return "launchd list unreadable"
        if jobs is not None and p in jobs:
            return "launchd job" if p == pid else "below launchd job %d" % p
        if v["tty"] not in ("??", "-", ""):
            return "has terminal" if p == pid else "below terminal session %d" % p
        if SYSTEM_PATH.match(v["cmd"]):
            return "app or system binary" if p == pid else "below app %d" % p
        if PROTECTED_NAME.search(exe_name(v["cmd"])) or PROTECTED_ARG.search(v["cmd"]):
            return "agent tool" if p == pid else "below agent tool %d" % p
    return None


def tree_of(procs, root):
    children = {}
    for p, v in procs.items():
        children.setdefault(v["ppid"], []).append(p)
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        if p not in out:
            out.append(p)
            todo.extend(children.get(p, []))
    return out


def has_sockets(pid):
    """True when lsof shows any internet socket, or cannot tell (so never stale-stopped)."""
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-i"],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return True
    if r.returncode not in (0, 1):
        return True
    return len(r.stdout.strip().splitlines()) > 1


def footprint_gb(pid):
    f = mg.facts(pid)
    return f[0] / mg.GB if f else 0.0


def classify(before, after, jobs, me_chain):
    """(auto, review, watch): lists of dicts for processes alive in both samples."""
    auto, review, watch = [], [], []
    for pid, v in after.items():
        b = before.get(pid)
        if b is None or b["cmd"] != v["cmd"]:
            continue
        cpu = max(0.0, v["cpu_s"] - b["cpu_s"]) / WINDOW_S * 100
        gb = footprint_gb(pid)
        exe = exe_name(v["cmd"])
        orphan = v["ppid"] == 1 and (jobs is not None and pid not in jobs)
        dev = bool(DEV_FAMILY.match(exe))
        item = {"pid": pid, "cmd": v["cmd"][:160], "age_h": round(v["age"] / 3600, 1), "cpu_pct": round(cpu),
                "gb": round(gb, 1)}
        why = protected_reason(after, pid, jobs, me_chain)
        hot, big = cpu >= RUNAWAY_CPU and v["age"] >= RUNAWAY_AGE_S, gb >= BIG_GB
        if why:
            if hot or big:
                watch.append(dict(item, protected=why))
            continue
        rule = None
        if orphan and dev:
            if hot:
                rule = "runaway orphan"
            elif SNAPSHOT_SHELL.match(v["cmd"]) and v["age"] >= SHELL_AGE_S:
                rule = "orphaned shell"
            elif v["age"] >= STALE_AGE_S and cpu < 1 and not has_sockets(pid):
                rule = "stale orphan"
        if rule:
            auto.append(dict(item, rule=rule))
        elif (orphan and dev and v["age"] >= REVIEW_AGE_S) or hot or big:
            ident = mg.identity(pid)
            if ident is not None:
                review.append(dict(item, ident=str(ident), orphan=orphan))
    return auto, review, watch


def bloat():
    out = []
    for d in BLOAT_DIRS:
        path = os.path.expanduser(d)
        if not os.path.exists(path):
            continue
        try:
            r = subprocess.run(["/usr/bin/du", "-sk", path], capture_output=True, text=True, timeout=20)
            kb = int(r.stdout.split()[0]) if r.stdout.split() else None
        except (subprocess.SubprocessError, OSError, ValueError):
            kb = None
        out.append({"dir": d, "gb": round(kb / 1024 / 1024, 1) if kb is not None else "timeout"})
    st = os.statvfs("/")
    out.append({"dir": "/ free", "gb": round(st.f_bavail * st.f_frsize / mg.GB, 1)})
    return out


def stop_tree(procs, root):
    targets = {}
    for p in tree_of(procs, root):
        ident = mg.identity(p)
        if ident is not None:
            targets[p] = ident
    return mg.stop_all(targets, GRACE_S) if targets else {}


def log(rec):
    rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if LOG.stat().st_size > LOG_CAP_BYTES:
        lines = LOG.read_text().splitlines(keepends=True)
        tmp = LOG.with_name(LOG.name + ".tmp")
        tmp.write_text("".join(lines[len(lines) // 2:]))
        os.replace(tmp, LOG)


def fmt(item, extra):
    return "- pid %d · %s · age %sh · cpu %s%% · %s GB · %s" % (
        item["pid"], extra, item["age_h"], item["cpu_pct"], item["gb"], item["cmd"])


def report(dry_run):
    signals = not dry_run and os.environ.get("ZS_SWEEP", "1") != "0"
    me_chain = set(mg.ancestors(mg.list_procs(), os.getpid())) | {os.getpid()}
    before = read_table()
    time.sleep(WINDOW_S)
    after = read_table()
    jobs = launchd_pids()
    auto, review, watch = classify(before, after, jobs, me_chain)
    for item in auto:
        item["result"] = stop_tree(after, item["pid"]) if signals else "dry run"
        log({"action": "auto-stop", **{k: v for k, v in item.items()}})
    b = bloat()
    comp = mg.compressor_bytes()
    log({"action": "report", "auto": len(auto), "review": len(review), "watch": len(watch),
         "compressed_gb": round(comp / mg.GB, 1) if comp else None, "bloat": b})
    lines = ["# sweep %s%s" % (time.strftime("%Y-%m-%d %H:%M"), "" if signals else " (no signals: dry run or ZS_SWEEP=0)"),
             "processes: %d · compressor: %s GB · load: %s" % (
                 len(after), round(comp / mg.GB, 1) if comp else "?", " ".join("%.1f" % x for x in os.getloadavg())),
             "", "## stopped automatically (%d)" % len(auto)]
    lines += [fmt(i, "%s -> %s" % (i["rule"], i["result"] if isinstance(i["result"], str)
                                   else ",".join(sorted(set(i["result"].values()))))) for i in auto] or ["- none"]
    lines += ["", "## review: you may stop these with `sweep.py stop PID IDENT` (%d)" % len(review)]
    lines += [fmt(i, "IDENT %s · %s" % (i["ident"], "orphan" if i["orphan"] else "has live parent"))
              for i in review] or ["- none"]
    lines += ["", "## watch: protected, report only (%d)" % len(watch)]
    lines += [fmt(i, i["protected"]) for i in watch] or ["- none"]
    lines += ["", "## disk (report only)"] + ["- %s: %s GB" % (x["dir"], x["gb"]) for x in b]
    print("\n".join(lines))
    return 0


def stop(pid, ident):
    try:
        pid = int(pid)
    except ValueError:
        print("refused: pid must be a number")
        return 2
    if os.environ.get("ZS_SWEEP", "1") == "0":
        print("refused: ZS_SWEEP=0")
        return 2
    procs = read_table()
    me_chain = set(mg.ancestors(mg.list_procs(), os.getpid())) | {os.getpid()}
    if pid not in procs or str(mg.identity(pid)) != ident:
        print("refused: pid %d is gone or is now a different process" % pid)
        return 2
    why = protected_reason(procs, pid, launchd_pids(), me_chain)
    if why:
        log({"action": "stop-refused", "pid": pid, "reason": why, "cmd": procs[pid]["cmd"][:160]})
        print("refused: pid %d is protected (%s)" % (pid, why))
        return 2
    result = stop_tree(procs, pid)
    log({"action": "stop", "pid": pid, "cmd": procs[pid]["cmd"][:160], "result": result})
    print("stopped pid %d and %d descendants: %s" % (pid, len(result) - 1, json.dumps(result)))
    return 0


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if not args or args[0] == "report":
        return report(dry)
    if args[0] == "stop" and len(args) == 3:
        return stop(args[1], args[2])
    print(__doc__.split("Usage:")[1].split("Log:")[0].rstrip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
