#!/usr/bin/env python3
"""rotate-logs.py — size-cap every launchd stdout/stderr redirect this user owns.

Why this exists (2026-08-01): launchd writes StandardOutPath/StandardErrorPath with
NO rotation anywhere — not in the plists, not in newsyslog (which needs root, so it
is not available to user agents). A survey found 80 redirect targets holding 87.6MB,
the worst single file 23.6MB, and one service (com.zs.ollama) that wrote 8.6MB of a
single repeated error in 36h. zs-memory's app/logconfig.py solves this INSIDE one
service; this covers the ~30 agents that merely redirect a file descriptor.

COPY-TRUNCATE, not rename: launchd holds the log open for the job's lifetime, so a
rename would leave the process writing to an unlinked inode and the "new" file
permanently empty. Truncating in place keeps that descriptor valid. Verified against
a live launchd job (2026-08-01): the file resumed at offset 0 with zero NUL padding,
confirming launchd opens these O_APPEND.

Ordering guarantees (the archive is durable BEFORE anything is destroyed):
  1. open the target ONCE with O_RDWR|O_NOFOLLOW and validate that fd via fstat
  2. stream it through gzip into a temp file in the same directory
  3. fsync the temp, os.replace() it onto a timestamped archive name, fsync the dir
  4. only then ftruncate(fd, 0) -- on the validated descriptor, never the path
  5. prune archives beyond the keep count
A crash before step 4 loses nothing; a crash after it finds the archive already
durable. Timestamped names mean there is no destructive .1->.2->.3 rename chain to
lose an old archive when a copy fails.

Hardened after an adversarial review (GPT-5.6 Sol, 2026-08-01) that found: concurrent
invocations overwriting each other's archive, a TOCTOU symlink swap between the
stat-check and the copy/truncate, hardlinked targets truncating every name for an
inode, backup renumbering destroying an archive on copy failure, unchecked
publication after truncation, and a non-dict plist root aborting the whole sweep.
"""
from __future__ import annotations

import fcntl
import glob
import gzip
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import time

MAX_BYTES = max(1, int(os.environ.get("ZS_ROTATE_MAX_BYTES", 10 * 1024 * 1024)))
# KEEP < 1 would make prune() delete every archive immediately after truncation,
# i.e. rotation would become deletion. Floor it.
KEEP = max(1, int(os.environ.get("ZS_ROTATE_KEEP", 3)))
AGENT_DIR = os.path.expanduser(os.environ.get("ZS_ROTATE_AGENT_DIR", "~/Library/LaunchAgents"))
STATE_LOG = os.path.expanduser(os.environ.get("ZS_ROTATE_LOG", "~/Library/Logs/zs-rotate-logs.jsonl"))
LOCK_PATH = os.path.expanduser("~/Library/Logs/.zs-rotate-logs.lock")
STATE_LOG_CAP = 2 * 1024 * 1024
DRY_RUN = "--dry-run" in sys.argv[1:]

ARCHIVE_RE = re.compile(r"\.\d{8}T\d{6}Z\.gz$")

# Directories owned by a package manager: their logs are not ours to rotate.
SKIP_PREFIXES = ("/opt/homebrew/", "/usr/local/Cellar/", "/opt/local/")

# Confinement. A plist is just a config file: a wrong or stale one could name a
# database, a document, or a backup, and this truncates whatever it is handed.
# All 71 real launchd targets on this machine end in .log/.err/.out (.jsonl is for
# the watchdog event log in EXTRA_LOGS), so requiring that suffix costs nothing and turns "truncate the user's sqlite file" into a logged
# skip. Anything new that legitimately needs rotating gets renamed or added here.
LOG_SUFFIXES = (".log", ".err", ".out", ".jsonl")

# Non-launchd logs worth capping. The Ollama GUI writes these directly rather than
# through launchd, so O_APPEND is NOT guaranteed — a non-append writer holding an
# old offset would write past the new EOF and leave a NUL hole. They are therefore
# rotated only when no process holds them open.
EXTRA_LOGS = [
    os.path.expanduser("~/.ollama/logs/app.log"),
    os.path.expanduser("~/.ollama/logs/server.log"),
    # watchdog.sh appends one line per component every sweep and never trims it
    # (22 MB by 2026-10-23). Short >> appends, so the open-holder check rarely skips.
    os.path.expanduser("~/Library/Logs/zs-watchdog.jsonl"),
]

# This job's own redirects. Rotating a file we are actively writing to is a
# needless self-dependency.
SELF_LOGS = {
    os.path.expanduser("~/Library/Logs/zs-rotate-logs.out.log"),
    os.path.expanduser("~/Library/Logs/zs-rotate-logs.err.log"),
}

events: list[dict] = []


def log_event(path: str, action: str, **extra) -> None:
    """One JSONL line per decision. json.dumps so a path containing quotes,
    backslashes or control characters cannot corrupt the audit file."""
    events.append(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "file": path,
         "action": action, **extra}
    )


def flush_events() -> None:
    if not events:
        return
    try:
        os.makedirs(os.path.dirname(STATE_LOG), exist_ok=True)
        # Cap the audit file itself; it is otherwise the one log nothing rotates.
        if os.path.exists(STATE_LOG) and os.path.getsize(STATE_LOG) > STATE_LOG_CAP:
            with open(STATE_LOG, errors="replace") as fh:
                tail = fh.readlines()[-2000:]
            with open(STATE_LOG, "w") as fh:
                fh.writelines(tail)
        with open(STATE_LOG, "a") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
    except OSError:
        pass  # never let audit-logging failure break the sweep


def in_git_worktree(path: str) -> bool:
    """A log inside a checkout would litter it with untracked archives (and the
    real bug is the plist writing a log there at all)."""
    d = os.path.dirname(path)
    while d and d != "/":
        if os.path.exists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return False


def _load_via_plutil(path: str):
    """Re-read a plist through plutil, which tolerates the non-strict XML that
    launchd also accepts. Returns the dict, or None if it is genuinely bad."""
    try:
        r = subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", path],
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not r.stdout:
            return None
        d = plistlib.loads(r.stdout)
        return d if isinstance(d, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError,
            plistlib.InvalidFileException):
        return None


def collect_targets() -> list[str]:
    """Every StandardOutPath/StandardErrorPath in this user's LaunchAgents.
    A malformed OR non-dict plist is skipped individually — one bad file must not
    take the whole sweep with it."""
    seen: set[str] = set()
    out: list[str] = []
    for p in sorted(glob.glob(os.path.join(AGENT_DIR, "*.plist"))):
        try:
            with open(p, "rb") as fh:
                d = plistlib.load(fh)
        except Exception as exc:
            # plistlib uses expat, which correctly rejects "--" inside an XML
            # comment; plutil and launchd both accept it. Several hand-written
            # plists here document a "--flag" in their header comment and are
            # therefore live-and-loaded but unparseable, which silently dropped
            # their logs from this sweep. Fall back to plutil's canonical XML.
            d = _load_via_plutil(p)
            if d is None:
                log_event(p, "plist-unreadable", error=str(exc)[:120])
                continue
        if not isinstance(d, dict):
            log_event(p, "plist-not-a-dict")
            continue
        for key in ("StandardOutPath", "StandardErrorPath"):
            f = d.get(key)
            if not isinstance(f, str) or not f.startswith("/"):
                continue  # launchd does not expand ~; a relative path is not ours to guess
            if f not in seen:
                seen.add(f)
                out.append(f)
    for f in EXTRA_LOGS:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def has_open_holders(path: str) -> bool:
    """True if any process holds the file open. FAIL-CLOSED: anything other than a
    clean 'no matches' answer counts as held, because the caller uses this to decide
    whether truncating under a possibly non-append writer is safe. lsof exits 1 for
    'no matches' and also for several error conditions, so stderr is checked too."""
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-t", "--", path],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    # rotate() holds its own descriptor on the file while asking, so lsof always
    # lists this process; without excluding it every EXTRA_LOGS file was skipped.
    if set(r.stdout.split()) - {str(os.getpid()).encode()}:
        return True
    if r.returncode not in (0, 1) or r.stderr.strip():
        return True  # errored rather than genuinely reporting no holders
    return False


def prune(path: str, just_published: str, live_targets: set) -> int:
    """Delete archives beyond KEEP. Returns the number of prune failures.

    glob.escape because a log path may legitimately contain [ or ? and an
    unescaped pattern would then match a DIFFERENT log's archives. The
    just-published archive is excluded unconditionally: if the clock has moved
    backwards, sorting by name would otherwise rank it oldest and delete the
    very archive this run just created."""
    failures = 0
    pattern = glob.escape(path) + ".*.gz"
    archives = sorted(
        g for g in glob.glob(pattern)
        if ARCHIVE_RE.search(g) and g != just_published and g not in live_targets
    )
    for old in archives[: max(0, len(archives) - (KEEP - 1))]:
        try:
            os.remove(old)
            log_event(old, "pruned")
        except OSError as exc:
            failures += 1
            log_event(old, "failed-prune", error=str(exc)[:120])
    return failures


def rotate(path: str, live_targets: set) -> str:
    """Returns one of: rotated / skipped-* / failed-* / absent / under-threshold."""
    if path in SELF_LOGS:
        return "skipped-self"
    if any(path.startswith(p) for p in SKIP_PREFIXES):
        return "skipped-package-manager"
    if ARCHIVE_RE.search(path):
        return "skipped-is-archive"
    if not path.endswith(LOG_SUFFIXES):
        log_event(path, "skipped-not-a-log-suffix")
        return "skipped-not-a-log-suffix"

    # Open ONCE, O_NOFOLLOW, and do every subsequent operation on this descriptor.
    # A symlink swapped in after the check cannot redirect the copy or the truncate.
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        # A job that has not run yet, or a /tmp log macOS already purged. Benign,
        # and common enough that lumping it in with real errors hides those.
        return "absent"
    except OSError as exc:
        # ELOOP here is O_NOFOLLOW rejecting a symlink — a guard firing, not a fault.
        log_event(path, "skipped-unopenable", error=str(exc)[:120])
        return "skipped-unopenable"

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "skipped-not-regular"
        if st.st_uid != os.getuid():
            return "skipped-not-owned"
        if st.st_nlink != 1:
            # A hardlink means truncating here empties every other name too.
            log_event(path, "skipped-hardlinked", nlink=st.st_nlink)
            return "skipped-hardlinked"
        if st.st_size < MAX_BYTES:
            return "under-threshold"
        if in_git_worktree(path):
            log_event(path, "skipped-in-git-worktree", bytes=st.st_size)
            return "skipped-in-git-worktree"
        if path in EXTRA_LOGS and has_open_holders(path):
            # Not launchd-managed, so O_APPEND is unproven; truncating under a
            # non-append writer would leave a NUL hole.
            log_event(path, "skipped-open-non-launchd", bytes=st.st_size)
            return "skipped-open-non-launchd"

        size = st.st_size
        if DRY_RUN:
            print(f"  WOULD ROTATE {size:>12,} B  {path}")
            return "would-rotate"

        d = os.path.dirname(path) or "."
        # Two rotations of one file inside the same second would otherwise generate
        # the same name and os.replace would destroy the first archive.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = f"{path}.{stamp}.gz"
        n = 1
        while os.path.lexists(archive):
            archive = f"{path}.{stamp}-{n}.gz"
            n += 1
        tmp = f"{archive}.tmp"

        # 1. stream fd -> gzip temp (O_EXCL so a pre-planted temp cannot be followed)
        try:
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            log_event(path, "failed-temp-create", error=str(exc)[:120])
            return "failed-temp-create"
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            # raw must close (flushing gzip's trailer) BEFORE the fsync below, so
            # the nesting order here is load-bearing, not stylistic.
            with os.fdopen(os.dup(tfd), "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw) as gz:
                    remaining = size
                    while remaining > 0:
                        # Copy exactly the bytes fstat saw. Chasing EOF instead would
                        # let a fast writer keep this loop (and the global lock) alive
                        # indefinitely, and those extra bytes get truncated away anyway.
                        chunk = os.read(fd, min(1024 * 1024, remaining))
                        if not chunk:
                            break  # file shrank under us; archive what exists
                        gz.write(chunk)
                        remaining -= len(chunk)
            os.fsync(tfd)
        except OSError as exc:
            try: os.remove(tmp)
            except OSError: pass
            log_event(path, "failed-copy", error=str(exc)[:120], bytes=size)
            return "failed-copy"
        finally:
            # finally, so an unexpected exception cannot leak the descriptor
            try: os.close(tfd)
            except OSError: pass

        # 2. publish atomically, then fsync the directory so the rename is durable
        try:
            os.replace(tmp, archive)
            dfd = os.open(d, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError as exc:
            try: os.remove(tmp)
            except OSError: pass
            log_event(path, "failed-publish", error=str(exc)[:120], bytes=size)
            return "failed-publish"

        # 3. the archive is durable; only now is it safe to destroy the live bytes
        try:
            os.ftruncate(fd, 0)
        except OSError as exc:
            log_event(path, "failed-truncate", error=str(exc)[:120], bytes=size)
            return "failed-truncate"

        log_event(path, "rotated", bytes=size, archive=os.path.basename(archive))
        print(f"  rotated {size:>12,} B -> {os.path.basename(archive)}")
        if prune(path, archive, live_targets):
            return "failed-prune"
        return "rotated"
    finally:
        os.close(fd)


def sweep_stale_temps(targets) -> None:
    """A SIGTERM during compression leaves a `.gz.tmp` behind. It is never
    published and never pruned, so without this it accumulates forever."""
    cutoff = time.time() - 6 * 3600
    for t in targets:
        for tmp in glob.glob(glob.escape(t) + ".*.gz.tmp"):
            try:
                if os.path.getmtime(tmp) < cutoff:
                    os.remove(tmp)
                    log_event(tmp, "removed-stale-temp")
            except OSError:
                pass


def main() -> int:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    # O_CREAT without O_TRUNC and with O_NOFOLLOW: opening "w" would truncate
    # whatever the lock path pointed at before the lock was ever acquired.
    try:
        lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        print(f"zs-rotate-logs: cannot open lock file: {exc}", file=sys.stderr)
        return 1
    try:
        # Serialize the whole sweep: two invocations racing could otherwise both
        # copy, both truncate, and leave the second archive holding one stray line.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print("zs-rotate-logs: another run holds the lock, exiting")
        return 0

    targets = collect_targets()
    live = set(targets)
    sweep_stale_temps(targets)
    counts: dict = {}
    for t in targets:
        r = rotate(t, live)
        counts[r] = counts.get(r, 0) + 1

    flush_events()
    # Collection problems are operational failures too — a plist this cannot read
    # is a log this silently stops capping.
    bad_plists = sum(1 for e in events if e["action"] in ("plist-unreadable", "plist-not-a-dict"))
    failed = sum(n for k, n in counts.items() if k.startswith("failed")) + bad_plists
    rotated = counts.get("rotated", 0) + counts.get("would-rotate", 0)
    notable = {k: v for k, v in counts.items()
               if k not in ("under-threshold", "skipped-self", "skipped-package-manager",
                            "absent")}
    print(f"zs-rotate-logs: {len(targets)} targets, {rotated} rotated"
          + (f", detail={notable}" if notable else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
