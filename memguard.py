#!/usr/bin/env python3
"""memguard.py — stop runaway test runners before the machine runs out of memory.

Written after the 2026-10-21 kernel panic. Seven agents ran the memo-reader vision
suite (birefnet, 12-30 GB per pytest process) at once; with llama-server they held
about 142 GB on this 48 GB Mac. The memory compressor went from 1.3 GB to 25 GB in
five minutes (03:00-03:05), jetsam killed system daemons, watchdogd stalled and the
kernel panicked between 03:10 and 03:16. memwatch saw it all and did nothing: its
"available" figure counts inactive pages and never fell below 6.6 GB.

Only test runners are ever stopped: a process of this user whose executable is
pytest / py.test / vitest / jest, a python running `-m pytest` or a pytest script, or
a node / bun running a vitest or jest script (is_runner). A word in a prompt or an
argument never makes a process a runner. Each runner is counted with all its
descendants (xdist, multiprocessing and vitest/jest workers) and stopped as one tree.
A runner inside another runner's tree, or one that is an ancestor of this process,
is never a separate target. Everything else is reported, never touched. Two rules:

  per-process cap  a runner tree above PROC_GB is stopped at once, whatever the
                   pressure. No test run on this machine needs that much.
  pressure         when the compressor holds more than COMPRESSED_GB, or the kernel
                   reports critical pressure, runner trees are stopped largest first
                   until their total is at or below KEEP_GB. The last tree is kept
                   unless the pressure is critical: one vision run (about 13 GB) fits.

Memory is the physical footprint (resident plus compressed) from proc_pid_rusage,
byte-accurate, not RSS: a process whose pages were compressed looks small in RSS
while it fills the compressor. The compressor size is the pages it occupies
(host_statistics64, the same number as vm_stat's "Pages occupied by compressor").

Stopping: every process of the chosen trees is frozen (SIGSTOP) and the trees are
read again, so a worker forked meanwhile is caught too. Then all get SIGTERM and
SIGCONT at once, and SIGKILL after one shared GRACE_S. The start time recorded with
the sample is checked before each signal, so a reused pid is never signalled. One copy runs at a time (flock). Each stop is logged to
~/Library/Logs/zs-memguard.jsonl before the signals and again with the results, and
shown as a desktop notification.

Called every 60 s from memwatch.py and every sweep from watchdog.sh (check_memguard).
Runs on /usr/bin/python3 (3.9), no third-party modules.

Tuning (env; an invalid number falls back to the default and is reported):
  ZS_MEMGUARD_PROC_GB        per-tree cap, GB                   (default 32)
  ZS_MEMGUARD_COMPRESSED_GB  compressor trigger, GB             (default 16)
  ZS_MEMGUARD_KEEP_GB        runner total kept under pressure   (default 16)
  ZS_MEMGUARD_GRACE_S        SIGTERM to SIGKILL, seconds        (default 5, at most 30)
  ZS_MEMGUARD_DRY_RUN        1 = decide and log, stop nothing
  ZS_MEMGUARD=0              turn the guard off

stdout is one JSON line. "status" is off, busy (another copy runs), ok, degraded
(a sensor, a runner's footprint, the lock or the config failed; the rest still
applied), dry_run, stopped or error (the process list could not be read; exit 1). "summary" is plain text without quotes.
"""

import ctypes
import ctypes.util
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG = Path.home() / "Library/Logs/zs-memguard.jsonl"
LOCK = Path.home() / "Library/Logs/.zs-memguard.lock"
LOG_CAP_BYTES = 2 * 1024 * 1024
GB = 1024 ** 3

RUNNERS = {"pytest", "py.test", "vitest", "jest"}
JS_RUNNER_SCRIPTS = {"vitest", "vitest.mjs", "jest", "jest.js"}
NUMBERS = (("proc_gb", "ZS_MEMGUARD_PROC_GB", 32.0, 1024), ("compressed_gb", "ZS_MEMGUARD_COMPRESSED_GB", 16.0, 1024),
           ("keep_gb", "ZS_MEMGUARD_KEEP_GB", 16.0, 1024),
           ("grace_s", "ZS_MEMGUARD_GRACE_S", 5.0, 30))  # both callers stop the guard after 60 s


def config():
    cfg, errors = {}, []
    for key, env, default, top in NUMBERS:
        raw = os.environ.get(env, "").strip()
        try:
            v = float(raw) if raw else default
        except ValueError:
            v = float("nan")
        if not (math.isfinite(v) and 0 < v <= top):
            errors.append("%s invalid, using %g" % (env, default))
            v = default
        cfg[key] = v
    cfg["dry_run"] = os.environ.get("ZS_MEMGUARD_DRY_RUN") == "1"
    cfg["errors"] = errors
    return cfg


PY_SCRIPT_RUNNERS = {"pytest", "py.test"}
NODE_OPTS_WITH_ARG = {"-r", "--require", "--import", "--loader", "--experimental-loader", "--title", "-C",
                      "--conditions", "--env-file", "--input-type", "--inspect-port", "--max-old-space-size"}
NODE_CODE_OPTS = {"-e", "--eval", "-p", "--print", "-i", "--interactive", "-"}


def _python_runs_pytest(args):
    """args after the interpreter: True for `-m pytest`, `-mpytest`, `-Impytest` or a pytest script."""
    i = 0
    while i < len(args):
        t = args[i]
        if t == "-" or t == "--":
            return t == "--" and i + 1 < len(args) and os.path.basename(args[i + 1]) in PY_SCRIPT_RUNNERS
        if t.startswith("--"):
            i += 1
            continue
        if t.startswith("-"):
            for k, ch in enumerate(t[1:], 1):  # a cluster of short options, e.g. -uIm or -Wignore
                rest = t[k + 1:]
                if ch == "c":
                    return False  # -c code: whatever follows is the program's argv
                if ch == "m":
                    mod = rest or (args[i + 1] if i + 1 < len(args) else "")
                    return mod in PY_SCRIPT_RUNNERS
                if ch in "WXQ":
                    i += 0 if rest else 1  # the value is attached or is the next token
                    break
            i += 1
            continue
        return os.path.basename(t) in PY_SCRIPT_RUNNERS
    return False


def _node_runs_runner(args, bun):
    i = 0
    if bun and args and args[0] == "test":
        return True  # bun's own test runner
    if bun and args and args[0] in ("run", "x"):
        i = 1
    while i < len(args):
        t = args[i]
        if t in NODE_CODE_OPTS:
            return False
        if t.startswith("-"):
            i += 2 if (t in NODE_OPTS_WITH_ARG and "=" not in t) else 1
            continue
        return os.path.basename(t) in JS_RUNNER_SCRIPTS
    return False


def is_runner(command):
    """True only when the executable, or the script or module it runs, is a test runner."""
    toks = command.split()
    if not toks:
        return False
    exe = os.path.basename(toks[0])
    if exe in RUNNERS:
        return True
    if re.match(r"^python[\d.]*$", exe, re.I):  # Homebrew shows .../Python.app/Contents/MacOS/Python
        return _python_runs_pytest(toks[1:])
    if re.match(r"^(node[\d.]*|bun)$", exe):
        return _node_runs_runner(toks[1:], exe == "bun")
    return False


def parse_ps(ps_out):
    """{pid: (ppid, command)} from `ps -o pid=,ppid=,command=`."""
    out = {}
    for line in ps_out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return out


def ancestors(procs, pid):
    chain, seen = [], set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = procs[pid][0]
    return chain


def runner_trees(procs, me):
    """[(root pid, [root and descendant pids])] for runners that are not inside another runner or above `me`."""
    children = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    runners = {pid for pid, (_, cmd) in procs.items() if is_runner(cmd)}
    mine = set(ancestors(procs, me)) | {me}
    trees = []
    for root in sorted(runners):
        if root in mine or any(a in runners for a in ancestors(procs, procs[root][0])):
            continue
        tree, todo = [], [root]
        while todo:
            p = todo.pop()
            if p not in tree and p not in mine:
                tree.append(p)
                todo.extend(children.get(p, []))
        trees.append((root, tree))
    return trees


def decide(trees, compressed, level, cfg):
    """trees: [(root, bytes, command, pids)]; compressed and level may be None (sensor failed).

    Returns (victims [(root, bytes, command, pids, rule)], pressure). Pure: no I/O.
    """
    trees = sorted(trees, key=lambda t: -t[1])
    capped = [t for t in trees if t[1] > cfg["proc_gb"] * GB]
    victims = [t + ("proc_cap",) for t in capped]
    critical = level is not None and level >= 4
    pressure = critical or (compressed is not None and compressed > cfg["compressed_gb"] * GB)
    if pressure:
        rest = [t for t in trees if t not in capped]
        total = sum(t[1] for t in rest)
        for i, t in enumerate(rest):  # largest first
            if total <= cfg["keep_gb"] * GB or (i == len(rest) - 1 and not critical):
                break
            victims.append(t + ("pressure",))
            total -= t[1]
    return victims, pressure


# --- macOS process facts (proc_pid_rusage, sysctlbyname); ps fallbacks elsewhere ---

class _Rusage(ctypes.Structure):  # struct rusage_info_v0
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins",
        "wired_size", "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime")]


def _load(path):
    try:
        return ctypes.CDLL(path, use_errno=True) if path else None
    except OSError:
        return None


_LIBPROC = _load("/usr/lib/libproc.dylib") if sys.platform == "darwin" else None
_LIBC = _load(ctypes.util.find_library("c")) if sys.platform == "darwin" else None


def _rusage(pid):
    if _LIBPROC is None:
        return None
    r = _Rusage()
    return r if _LIBPROC.proc_pid_rusage(int(pid), 0, ctypes.byref(r)) == 0 else None


def facts(pid):
    """(footprint bytes, identity) from one proc_pid_rusage read; None when gone, a zombie or not ours."""
    r = _rusage(pid)
    if r is None or r.proc_exit_abstime != 0:
        return None
    return r.phys_footprint, r.proc_start_abstime


def identity(pid):
    """A start-time token for a live process; None when it is gone, a zombie, not ours or unreadable."""
    if _LIBPROC is not None:
        f = facts(pid)
        return f[1] if f else None
    try:
        out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None  # cannot verify it: never signal it
    return out.split(None, 1)[1] if out and not out.startswith("Z") and " " in out else None


def sysctl_int(name):
    if _LIBC is None:
        return None
    v, size = ctypes.c_uint64(0), ctypes.c_size_t(8)
    if _LIBC.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return None
    return v.value


class _VM64(ctypes.Structure):  # struct vm_statistics64; checked against vm_stat on 2026-10-21
    _fields_ = [(n, ctypes.c_uint32) for n in ("free", "active", "inactive", "wire")] + \
        [(n, ctypes.c_uint64) for n in ("zero_fill", "reactivations", "pageins", "pageouts", "faults",
                                        "cow_faults", "lookups", "hits", "purges")] + \
        [(n, ctypes.c_uint32) for n in ("purgeable", "speculative")] + \
        [(n, ctypes.c_uint64) for n in ("decompressions", "compressions", "swapins", "swapouts")] + \
        [(n, ctypes.c_uint32) for n in ("compressor_page_count", "throttled", "external", "internal")] + \
        [("uncompressed_in_compressor", ctypes.c_uint64)]


def compressor_bytes():
    """Physical memory the compressor occupies (vm_stat "Pages occupied by compressor"); None if unreadable."""
    if _LIBC is None:
        return None
    page = sysctl_int("hw.pagesize")
    _LIBC.mach_host_self.restype = ctypes.c_uint32
    info, count = _VM64(), ctypes.c_uint32(ctypes.sizeof(_VM64) // 4)
    if not page or _LIBC.host_statistics64(_LIBC.mach_host_self(), 4, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return info.compressor_page_count * page


class SampleError(Exception):
    pass


def list_procs():
    """{pid: (ppid, command)} for this user's processes."""
    try:
        r = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as e:
        raise SampleError("ps: %s" % e)
    procs = parse_ps(r.stdout)
    if r.returncode != 0 or not procs:
        raise SampleError("ps exit %d, %d processes" % (r.returncode, len(procs)))
    return procs


def sample():
    """(procs {pid: (ppid, command)}, compressed bytes or None, pressure level or None)."""
    return list_procs(), compressor_bytes(), sysctl_int("kern.memorystatus_vm_pressure_level")


def new_descendants(targets, me):
    """{pid: identity} of live processes below any target that are not targets yet (forked after the sample)."""
    try:
        procs = list_procs()
    except SampleError:
        return {}
    skip = set(ancestors(procs, me)) | {me}
    out = {}
    for pid in procs:
        if pid in targets or pid in skip:
            continue
        if any(a in targets for a in ancestors(procs, procs[pid][0])):
            ident = identity(pid)
            if ident is not None:
                out[pid] = ident
    return out


def stop_all(targets, grace_s, refresh=None):
    """targets {pid: identity}. Freeze every target (SIGSTOP), add processes forked meanwhile (refresh),
    then SIGTERM + SIGCONT to all at once, one shared grace, SIGKILL to survivors.

    Returns {pid: gone | denied | terminated | killed}. A pid whose identity changed is never signalled.
    """
    targets, result = dict(targets), {}

    def send(pid, sig):
        if identity(pid) != targets[pid]:
            return "gone"
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "denied"
        return None

    pending = dict(targets)
    for _ in range(4):  # a frozen tree cannot fork; a few rounds catch workers spawned in between
        for pid in pending:
            r = send(pid, signal.SIGSTOP)
            if r:
                result[pid] = r
        pending = refresh(targets) if refresh else {}
        if not pending:
            break
        targets.update(pending)
    for pid in targets:
        if pid not in result:
            r = send(pid, signal.SIGTERM)
            if r:
                result[pid] = r
            else:
                send(pid, signal.SIGCONT)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and any(p not in result and identity(p) == targets[p] for p in targets):
        time.sleep(0.25)
    for pid in targets:
        if pid not in result:
            result[pid] = (send(pid, signal.SIGKILL) or "killed") if identity(pid) == targets[pid] else "terminated"
    return result


def notify(msg):
    try:  # not waited on: a slow osascript must not hold the guard
        subprocess.Popen(["/usr/bin/osascript", "-e", 'display notification "%s" with title "zs-memguard"'
                          % re.sub(r'["\\]', "'", msg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def append_log(rec):
    """Append one line; past LOG_CAP_BYTES keep the newer half via an atomic replace. Caller holds LOCK."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if LOG.stat().st_size > LOG_CAP_BYTES:
        lines = LOG.read_text().splitlines(keepends=True)
        tmp = LOG.with_name(LOG.name + ".tmp")
        tmp.write_text("".join(lines[len(lines) // 2:]))
        os.replace(tmp, LOG)


def _name(cmd):
    """The runner's name (pytest, vitest, jest) rather than the interpreter's."""
    names = [re.sub(r"^-\w*m|\.m?js$", "", os.path.basename(t)) for t in cmd.split()]
    return next((n for n in names if n in RUNNERS), names[0] if names else "?")


def _emit(out, code=0):
    out["summary"] = re.sub(r"[^\w .,:()=/+-]", "", out.get("summary", ""))[:200]
    print(json.dumps(out))
    return code


def _lock():
    """(open lock file or None, error or None). Contention returns ("busy", None)."""
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        fh = LOCK.open("a")
    except OSError as e:
        return None, "lock: %s" % e
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return "busy", None
    except OSError as e:
        fh.close()
        return None, "lock: %s" % e
    return fh, None


def main(me=None):
    if os.environ.get("ZS_MEMGUARD") == "0":
        return _emit({"status": "off", "summary": "ZS_MEMGUARD=0"})
    me = os.getpid() if me is None else me
    lock, lock_error = _lock()
    if lock == "busy":
        return _emit({"status": "busy", "summary": "another memguard is running"})
    try:  # a lock error never skips the guard: run unlocked and report it
        return _guard(me, [lock_error] if lock_error else [])
    finally:
        if lock is not None:
            lock.close()


def _guard(me, errors):
    cfg = config()
    errors += cfg["errors"]
    try:
        procs, compressed, level = sample()
    except SampleError as e:
        return _emit({"status": "error", "summary": str(e), "errors": errors + [str(e)]}, 1)
    if compressed is None:
        errors.append("compressor size unreadable")
    if level is None:
        errors.append("kern.memorystatus_vm_pressure_level unreadable")

    trees, unreadable = [], 0
    for root, pids in runner_trees(procs, me):
        seen = {p: facts(p) for p in pids}
        if seen[root] is None:
            unreadable += 1  # the runner itself, not a worker that just exited
        idents = {p: f[1] for p, f in seen.items() if f is not None}
        if idents:
            trees.append((root, sum(f[0] for f in seen.values() if f is not None), procs[root][1], idents))
    if unreadable:
        errors.append("%d runner footprint(s) unreadable" % unreadable)
    victims, pressure = decide(trees, compressed, level, cfg)

    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "compressed_gb": None if compressed is None else round(compressed / GB, 1),
        "pressure_level": level,
        "pressure": pressure,
        "dry_run": cfg["dry_run"],
        "runners": [{"pid": r, "gb": round(b / GB, 1), "procs": len(p), "command": c[:120]}
                    for r, b, c, p in sorted(trees, key=lambda t: -t[1])[:8]],
        "stopped": [],
        "errors": errors,
    }
    if victims:
        rec["stopped"] = [{"pid": r, "gb": round(b / GB, 1), "rule": rule, "procs": len(p), "command": c[:300]}
                          for r, b, c, p, rule in victims]
        names = ", ".join("%s pid %d (%.0f GB)" % (_name(c), r, b / GB) for r, b, c, _, _ in victims)
        verb = "Would stop" if cfg["dry_run"] else "Stopping"
        try:
            append_log(dict(rec, phase="dry_run" if cfg["dry_run"] else "stopping"))
        except OSError as e:
            errors.append("log: %s" % e)
        if not cfg["dry_run"]:
            targets = {}
            for _, _, _, idents, _ in victims:
                targets.update(idents)  # identities from the sample, never re-read here
            results = stop_all(targets, cfg["grace_s"])  # rescan off: round-3 review, frozen trees can outlive a stalled ps
            for row, (_, _, _, idents, _) in zip(rec["stopped"], victims):
                row["result"] = {str(p): results.get(p, "gone") for p in idents}
            late = sorted(p for p in results if p not in targets)
            if late:
                rec["late_workers"] = {str(p): results[p] for p in late}
            try:
                append_log(dict(rec, phase="done"))
            except OSError as e:
                errors.append("log: %s" % e)
            verb = "Stopped"
        notify("%s %d test run(s): %s" % (verb, len(victims), names))
        status, summary = ("dry_run" if cfg["dry_run"] else "stopped"), "%s %s" % (verb.lower(), names)
    else:
        status = "degraded" if errors else "ok"
        summary = "; ".join(errors) if errors else "%d runner tree(s), %.1f GB" % (
            len(trees), sum(t[1] for t in trees) / GB)
    out = {k: rec[k] for k in ("compressed_gb", "pressure_level", "pressure", "stopped", "errors")}
    out.update(status=status, summary=summary)
    return _emit(out)


if __name__ == "__main__":
    sys.exit(main())
