#!/usr/bin/env python3
"""org_archive — verified cold archive of one Inbox item to the DevinBackUp drive.

Usage:
  org_archive.py archive <path> --project <slug> [--dest-root DIR] [--keep-source] [--dry-run] [--allow-outside-inbox]
  org_archive.py restore <pointer.archived.md | archive.tar.zst> <target-dir>

Rules (design §8, hardened after the GPT-6.1 Sol review of 2026-10-13):
- The source must be a real path inside ~/Inbox (no symlink anywhere on the way) and must not
  be, contain or sit inside a git repo.
- One archive at a time (lock file). The archive is written to a unique temp name, flushed with
  F_FULLFSYNC, tested (zstd -t), listed, and its member paths must equal the source inventory
  exactly. tar must print nothing on stderr. mac metadata (xattrs, resource forks) is not kept.
- The source inventory (path, type, size, mtime_ns, ctime_ns, inode) is taken before and after;
  any difference keeps the source.
- A "prepare" manifest record is written and synced before the source is touched, the pointer
  is created exclusively (never overwrites), then the source is removed and "done" is logged.
- Restore checks sha256, extracts into an empty staging dir and never replaces existing paths.
"""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
INBOX = HOME / "Inbox"
DRIVE = Path("/Volumes/DevinBackUp")
DRIVE_UUID = "41B2D56C-3C82-35A6-A75B-ED3210EAA8DC"
ARCHIVE_ROOT = DRIVE / "Mac-Archive"
ORG_DIR = Path(__file__).resolve().parent
MANIFEST = ORG_DIR / "manifests" / "archive.jsonl"
LOCK = ORG_DIR / "state" / "archive.lock"
ZSTD = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
TAR = "/usr/bin/tar"
TAR_FLAGS = ["--no-mac-metadata", "--no-xattrs"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def full_sync(fd):
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except OSError:
        os.fsync(fd)


def sync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        full_sync(fd)
    finally:
        os.close(fd)


def log(entry):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    entry["at"] = now()
    fd = os.open(MANIFEST, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(entry) + "\n").encode())
        full_sync(fd)
    finally:
        os.close(fd)


@contextmanager
def archive_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def drive_ok():
    if not DRIVE.is_dir() or not os.path.ismount(DRIVE):
        return False, "drive not mounted"
    out = subprocess.run(["diskutil", "info", str(DRIVE)], capture_output=True, text=True).stdout
    if DRIVE_UUID not in out:
        return False, "drive identity mismatch"
    return True, ""


def check_source(src, allow_outside=False):
    """Return an error string, or "" when the path is safe to archive or quarantine."""
    p = Path(os.path.abspath(src))
    if not os.path.lexists(p):
        return "source missing"
    if os.path.realpath(p) != str(p):
        return "source or an ancestor is a symlink"
    if not allow_outside:
        inbox = os.path.realpath(INBOX)
        if not str(p).startswith(inbox + os.sep):
            return "source is outside ~/Inbox"
    for anc in [p, *p.parents]:
        if (anc / ".git").exists():
            return f"inside a git repo ({anc})"
        if anc == HOME:
            break
    if p.is_dir():
        for root, dirs, files in os.walk(p):
            if ".git" in dirs or ".git" in files:
                return f"contains a git repo ({root})"
    return ""


def inventory(src):
    """{relative path: (kind, size, mtime_ns, ctime_ns, ino)} for every non-directory entry."""
    src = Path(src)
    if not src.is_dir() or src.is_symlink():
        st = os.lstat(src)
        return {src.name: ("l" if src.is_symlink() else "f", st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)}
    inv = {}
    for root, dirs, files in os.walk(src):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            full = os.path.join(root, name)
            st = os.lstat(full)
            rel = os.path.join(src.name, os.path.relpath(full, src))
            inv[rel] = ("l" if os.path.islink(full) else "f", st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
    return inv


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_members(archive_path):
    """(ok, set of non-directory member paths). ok only if zstd -t, zstd -dc and tar all succeed."""
    if subprocess.run([ZSTD, "-q", "-t", str(archive_path)]).returncode != 0:
        return False, set()
    z = subprocess.Popen([ZSTD, "-q", "-dc", str(archive_path)], stdout=subprocess.PIPE)
    t = subprocess.run([TAR, "-tf", "-"], stdin=z.stdout, capture_output=True)
    z.stdout.close()
    ok = z.wait() == 0 and t.returncode == 0 and not t.stderr.strip()
    names = set()
    for raw in t.stdout.decode("utf-8", "surrogateescape").split("\n"):
        if not raw or raw.endswith("/"):
            continue
        names.add(raw[2:] if raw.startswith("./") else raw)
    return ok, names


def write_pointer(pointer, project, src_name, dest, digest, inv):
    """Create the pointer exclusively (no overwrite, no symlink follow). Raises OSError."""
    fd = os.open(pointer, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    restore_cmd = shlex.join(["/usr/bin/python3", str(Path(__file__).resolve()), "restore", str(pointer),
                              str(Path(pointer).parent)])
    body = (f"---\ntype: ArchivePointer\nproject: {project}\narchived_at: {now()}\n"
            f"archive_json: {json.dumps(str(dest))}\nsha256: {digest}\n---\n\n"
            f"# {src_name} (archived)\n\n- Archive: `{dest}`\n- sha256: `{digest}`\n"
            f"- Files: {len(inv)}, bytes: {sum(v[1] for v in inv.values())}\n"
            f"- Restore: `{restore_cmd}` (needs the DevinBackUp drive)\n")
    try:
        os.write(fd, body.encode())
        full_sync(fd)
    finally:
        os.close(fd)


def archive(src, project, dest_root=None, keep_source=False, dry_run=False, allow_outside=False):
    src = Path(os.path.abspath(src))
    err = check_source(src, allow_outside)
    if err:
        return {"ok": False, "error": err, "src": str(src)}
    if dest_root is None:
        ok, why = drive_ok()
        if not ok:
            return {"ok": False, "error": why, "src": str(src)}
    month = datetime.fromtimestamp(os.lstat(src).st_mtime).strftime("%Y-%m")
    dest_dir = Path(dest_root) if dest_root else ARCHIVE_ROOT / "inbox" / project / month
    dest = dest_dir / (src.name + ".tar.zst")
    pointer = src.parent / (src.name + ".archived.md")
    if os.path.lexists(dest):
        return {"ok": False, "error": "destination exists", "dest": str(dest)}
    if not keep_source and os.path.lexists(pointer):
        return {"ok": False, "error": "pointer path exists", "pointer": str(pointer)}
    before = inventory(src)
    total = sum(v[1] for v in before.values())
    if dry_run:
        return {"ok": True, "dry_run": True, "src": str(src), "dest": str(dest), "files": len(before), "bytes": total}
    with archive_lock():
        if os.path.lexists(dest):
            return {"ok": False, "error": "destination exists", "dest": str(dest)}
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".{src.name}.{os.getpid()}-{secrets.token_hex(4)}.partial"
        try:
            # "./name" keeps a basename that starts with "-" from being read as a tar option.
            tar = subprocess.Popen([TAR, *TAR_FLAGS, "-cf", "-", "-C", str(src.parent), "./" + src.name],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            z = subprocess.run([ZSTD, "-q", "-T0", "-10", "-o", str(tmp)], stdin=tar.stdout)
            tar.stdout.close()
            tar_err = tar.stderr.read()
            if tar.wait() != 0 or z.returncode != 0 or tar_err.strip():
                return {"ok": False, "error": f"tar/zstd failed: {tar_err.decode(errors='replace')[:300]}",
                        "src": str(src)}
            sync_path(tmp)
            ok, members = list_members(tmp)
            if not ok or members != set(before):
                missing = sorted(set(before) - members)[:5]
                extra = sorted(members - set(before))[:5]
                return {"ok": False, "error": f"member mismatch (ok={ok}, missing {missing}, extra {extra})",
                        "src": str(src)}
            digest = sha256(tmp)
            if os.path.lexists(dest):
                return {"ok": False, "error": "destination appeared during archive", "dest": str(dest)}
            os.rename(tmp, dest)
            sync_path(dest_dir)
        finally:
            if os.path.lexists(tmp):
                tmp.unlink()
        if sha256(dest) != digest:
            return {"ok": False, "error": "published archive hash mismatch; source kept", "dest": str(dest)}
        undo = shlex.join(["/usr/bin/python3", str(Path(__file__).resolve()), "restore", str(dest), str(src.parent)])
        entry = {"op": "archive", "src": str(src), "dest": str(dest), "sha256": digest, "files": len(before),
                 "bytes": total, "project": project, "source_kept": keep_source, "undo": undo}
        if inventory(src) != before or check_source(src, allow_outside):
            log({**entry, "state": "aborted", "reason": "source changed during archive; kept"})
            return {"ok": False, "error": "source changed during archive; kept", "dest": str(dest)}
        if keep_source:
            log({**entry, "state": "done"})
            return {**entry, "ok": True}
        log({**entry, "state": "prepare"})
        try:
            write_pointer(pointer, project, src.name, dest, digest, before)
        except OSError as e:
            log({**entry, "state": "aborted", "reason": f"pointer: {e}"})
            return {"ok": False, "error": f"pointer could not be created: {e}; source kept", "dest": str(dest)}
        if src.is_dir() and not src.is_symlink():
            shutil.rmtree(src)
        else:
            src.unlink()
        entry["pointer"] = str(pointer)
        log({**entry, "state": "done"})
        return {**entry, "ok": True}


def archive_from_pointer(pointer):
    text = Path(pointer).read_text()
    for line in text.splitlines():
        if line.startswith("archive_json: "):
            return Path(json.loads(line[len("archive_json: "):])), ""
    for line in text.splitlines():  # pointers written before the 2026-10-13 hardening
        if line.startswith("- Archive: `"):
            return Path(line[len("- Archive: `"):].rsplit("`", 1)[0]), ""
    return None, "no archive path in pointer"


def expected_sha(archive_path):
    if not MANIFEST.exists():
        return None
    found = None
    for line in MANIFEST.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("op") == "archive" and e.get("dest") == str(archive_path) and e.get("sha256"):
            found = e["sha256"]
    return found


def restore(archive_or_pointer, target):
    p = Path(archive_or_pointer)
    if p.suffix == ".md":
        p, err = archive_from_pointer(p)
        if err:
            print(err, file=sys.stderr)
            return False
    want = expected_sha(p)
    if want and sha256(p) != want:
        print(f"sha256 mismatch for {p}", file=sys.stderr)
        log({"op": "restore", "archive": str(p), "target": str(target), "ok": False, "error": "sha256 mismatch"})
        return False
    ok, _ = list_members(p)
    if not ok:
        print(f"archive failed integrity test: {p}", file=sys.stderr)
        return False
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    stage = target / f".restore-{secrets.token_hex(4)}"
    stage.mkdir()
    keep_stage = False
    try:
        z = subprocess.Popen([ZSTD, "-q", "-dc", str(p)], stdout=subprocess.PIPE)
        t = subprocess.run([TAR, "-xpf", "-", "-C", str(stage)], stdin=z.stdout)
        z.stdout.close()
        if z.wait() != 0 or t.returncode != 0:
            print("extraction failed; target untouched", file=sys.stderr)
            return False
        children = list(stage.iterdir())
        clashes = [c.name for c in children if os.path.lexists(target / c.name)]
        if clashes:
            keep_stage = True
            print(f"refusing to overwrite {clashes}; extracted copy left in {stage}", file=sys.stderr)
            log({"op": "restore", "archive": str(p), "target": str(target), "ok": False, "staged": str(stage)})
            return False
        for c in children:
            os.rename(c, target / c.name)
            pointer = target / (c.name + ".archived.md")
            if pointer.is_file() and not pointer.is_symlink():
                pointer.unlink()
        log({"op": "restore", "archive": str(p), "target": str(target), "ok": True})
        return True
    finally:
        if not keep_stage and stage.exists():
            shutil.rmtree(stage)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive")
    a.add_argument("path")
    a.add_argument("--project", default="_unassigned")
    a.add_argument("--dest-root")
    a.add_argument("--keep-source", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--allow-outside-inbox", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("target")
    args = ap.parse_args()
    if args.cmd == "archive":
        res = archive(args.path, args.project, args.dest_root, args.keep_source, args.dry_run,
                      args.allow_outside_inbox)
        print(json.dumps(res))
        sys.exit(0 if res.get("ok") else 1)
    sys.exit(0 if restore(args.archive, args.target) else 1)


if __name__ == "__main__":
    main()
