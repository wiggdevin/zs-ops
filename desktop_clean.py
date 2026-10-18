#!/usr/bin/env python3
"""desktop_clean.py - keep ~/Desktop down to the Projects and Inbox aliases
(launchd com.zs.desktop-clean, every 8 hours).

Every item on the Desktop other than the two aliases is handled in one pass:

  1. Aliases and symlinks (pointers, no data of their own) go to the Trash.
  2. Exact duplicates go to the Trash: a file whose bytes (sha256) already exist in ~/Inbox
     or in another Desktop file. One copy always survives.
  3. Installers whose app is already in /Applications go to the Trash (a .dmg is mounted
     read-only to find its .app).
  4. Items that share a leading name token (3 or more, e.g. mug35_*) are consolidated into one
     bundle folder; a later run adds new items to the same bundle.
  5. Everything else, folders included, moves to ~/Inbox/<type>/<project>/<YYYY-MM>/ under the
     name YYYY-MM-DD-<project>-<subject>.<ext> (project: registry alias, then Jev).

Never touches git repos or anything modified in the last 10 minutes, never overwrites, and
never deletes outright: Trash is a no-replace rename of the item itself (never what a link
points to) into ~/.Trash, re-checked right before it happens, and every action is written to a
manifest and journal (undo: org/org_router.py undo --manifest F). It shares the sweep lock with
inbox_sweep.py, which no longer handles the Desktop.

Usage: desktop_clean.py [--dry-run] [--no-jev]
Log:   ~/Library/Logs/zs-ai-os/desktop-clean.log
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "org"))
import inbox_paths as ip  # noqa: E402
import org_router as rt  # noqa: E402

HOME = Path.home()
DESKTOP = HOME / "Desktop"
TRASH = HOME / ".Trash"
APPLICATIONS = [Path("/Applications"), HOME / "Applications"]
ORG = HERE / "org"
STATE_DIR = ORG / "state"
MANIFEST_DIR = ORG / "manifests"
LOG = HOME / "Library/Logs/zs-ai-os/desktop-clean.log"
DESKTOP_KEEP = {"Projects", "Inbox"}
PARTIAL_EXTS = {".crdownload", ".download", ".part", ".partial", ".tmp"}
INSTALLER_EXTS = {".dmg", ".pkg"}
GROUP_MIN = 3
# leading tokens too common to mean "these belong together"
GENERIC_TOKENS = {"screenshot", "screen", "image", "img", "untitled", "copy", "new", "file",
                  "document", "doc", "export", "download", "photo", "video", "recording",
                  "design", "project", "test", "final", "draft", "the", "my"}

ROUTES = {".html": "html", ".htm": "html", ".md": "notes", ".markdown": "notes", ".txt": "notes",
          ".pdf": "pdfs"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
SCREENSHOT_RE = re.compile(r"(screenshot|screen shot|screen recording)", re.IGNORECASE)
DATE_IN_NAME = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
ALREADY_NAMED = re.compile(r"^20\d{2}-\d{2}-\d{2}-")


def configure(home: Path = None, inbox: Path = None) -> None:
    """Point every path at a different home/inbox (tests)."""
    global HOME, DESKTOP, TRASH, APPLICATIONS, LOG
    if home is not None:
        HOME = Path(home)
        DESKTOP = HOME / "Desktop"
        TRASH = HOME / ".Trash"
        APPLICATIONS = [HOME / "Applications"]
        LOG = HOME / "Library/Logs/zs-ai-os/desktop-clean.log"
    ip.INBOX = Path(inbox) if inbox is not None else (HOME / "Inbox")


# ----------------------------------------------------------------- helpers
def sha256_any(path: Path):
    """sha256 hex of a regular file of any size, else None."""
    h = hashlib.sha256()
    try:
        with open(str(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def inbox_size_index(inbox: Path):
    """{size: [paths]} for every regular file under the Inbox (stat only; hashes come later)."""
    by_size = {}
    for root, dirs, files in os.walk(str(inbox)):
        dirs[:] = [d for d in dirs if d not in rt.SKIP_DIRS_IN_WALK]
        for n in files:
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_size and os.path.isfile(p) and not os.path.islink(p):
                by_size.setdefault(st.st_size, []).append(p)
    return by_size


def inbox_copy_of(path: Path, size: int, digest: str, by_size):
    """An Inbox file with the same bytes as path, or None."""
    for cand in by_size.get(size, []):
        if sha256_any(Path(cand)) == digest:
            return cand
    return None


def route(path: Path) -> str:
    """Inbox type for a Desktop item. Unknown file types go to misc (nothing stays behind)."""
    if path.is_dir():
        kinds = {}
        for root, dirs, files in os.walk(str(path)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in files:
                if not n.startswith("."):
                    t = route(Path(n))
                    kinds[t] = kinds.get(t, 0) + 1
        if not kinds:
            return "misc"
        top, count = max(kinds.items(), key=lambda kv: kv[1])
        return top if count * 2 > sum(kinds.values()) else "misc"
    ext = path.suffix.lower()
    if ext in ROUTES:
        return ROUTES[ext]
    if ext in IMAGE_EXTS or ext in {".mov", ".mp4"}:
        return "screenshots" if SCREENSHOT_RE.search(path.name) else "misc"
    return "misc"


def item_date(name: str, mtime: float) -> _dt.date:
    m = DATE_IN_NAME.search(name)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return _dt.date.fromtimestamp(mtime)


def clean_name(name: str, project: str, day: _dt.date, is_dir: bool) -> str:
    """YYYY-MM-DD-<project>-<subject>[.ext]; the _unassigned slug is left out (inbox_paths rule)."""
    if ALREADY_NAMED.match(name):
        return name
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    stem = DATE_IN_NAME.sub(" ", stem)
    stem = re.sub(r"\bat\b", " ", stem) if SCREENSHOT_RE.search(stem) else stem
    subj = ip.slugify(stem)
    parts = [day.isoformat()]
    if project != ip.UNASSIGNED and not (subj == project or subj.startswith(project + "-")):
        parts.append(project)
    parts.append(subj)
    ext = ext.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or "") else ext
    return "-".join(parts) + ext


def lead_token(name: str):
    """First alphanumeric token of a name (lowercase), for grouping; None when generic."""
    m = re.match(r"[^A-Za-z0-9]*([A-Za-z]+[0-9]*|[0-9]+[A-Za-z]+[0-9]*)", name)
    if not m:
        return None
    tok = m.group(1).lower()
    if len(tok) < 3 or tok in GENERIC_TOKENS:
        return None
    return tok


def trash_path(path: Path):
    """Move path itself (never what it points to) into ~/.Trash. Returns (dst, error)."""
    try:
        TRASH.mkdir(exist_ok=True)
        dst = TRASH / path.name
        n = 2
        while os.path.lexists(str(dst)):
            dst = TRASH / ("%s %s-%d%s" % (path.stem, time.strftime("%H.%M.%S"), n, path.suffix))
            n += 1
        rt.rename_noreplace(str(path), str(dst))
        return str(dst), None
    except OSError as exc:
        return None, "trash failed: %s" % (exc.strerror or type(exc).__name__)


def has_resource_fork(path: Path) -> bool:
    """A file whose resource fork holds data is never called a duplicate (sha256 sees the data
    fork only). Unreadable counts as having one."""
    try:
        return os.path.getsize(str(path) + "/..namedfork/rsrc") > 0
    except FileNotFoundError:
        return False
    except OSError:
        return True


def dmg_apps(dmg: Path, runner=subprocess.run):
    """Names of the .app bundles at the top of a disk image (mounted read-only), or None.
    The image is always detached; when the detach fails the answer is None (not trusted)."""
    mnt = tempfile.mkdtemp(prefix="zs-dmg-")
    apps = None
    try:
        out = runner(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                      "-noverify", "-mountpoint", mnt, str(dmg)],
                     capture_output=True, text=True, timeout=120, input="Y\n")
        if out.returncode == 0:
            apps = []  # every .app on the image, nested ones included (not inside an .app)
            for root, dirs, files in os.walk(mnt):
                apps += [d for d in dirs if d.endswith(".app")]
                dirs[:] = [d for d in dirs if not d.endswith(".app") and not d.startswith(".")]
            apps = sorted(set(apps))
    except (OSError, subprocess.TimeoutExpired):
        apps = None
    finally:
        if os.path.ismount(mnt):
            try:
                det = runner(["/usr/bin/hdiutil", "detach", mnt, "-force"],
                             capture_output=True, text=True, timeout=60)
                if det.returncode != 0:
                    apps = None
            except (OSError, subprocess.TimeoutExpired):
                apps = None
        try:
            os.rmdir(mnt)
        except OSError:
            apps = None if os.path.ismount(mnt) else apps
    return apps


def installed_app_for(installer: Path, apps_of=dmg_apps):
    """The installed app this installer provides, or None. A .pkg is matched by name only."""
    installed = {}
    for d in APPLICATIONS:
        try:
            for n in os.listdir(str(d)):
                if n.endswith(".app"):
                    installed[n.lower()] = str(d / n)
        except OSError:
            continue
    if installer.suffix.lower() == ".dmg":
        apps = apps_of(installer) or []
        # disposable only when every app on the image is installed
        if apps and all(a.lower() in installed for a in apps):
            return ", ".join(installed[a.lower()] for a in apps)
        return None
    base = re.split(r"[-_ ]\d", installer.stem, maxsplit=1)[0].lower()
    return installed.get(base + ".app") if base else None


# -------------------------------------------------------------------- plan
def fingerprint(st):
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_mode]


def still_disposable(t):
    """Re-check a trash row right before the move: same file as planned, and for a duplicate the
    surviving copy still exists with the same bytes. Returns a reason when it is not."""
    try:
        st = os.lstat(t["src"])
    except OSError:
        return "gone"
    if fingerprint(st) != t.get("fp"):
        return "changed since planning"
    if t["reason"] == "duplicate":
        c = t["copy_of"]
        if os.path.islink(c) or not os.path.isfile(c) or has_resource_fork(Path(c)) \
                or has_resource_fork(Path(t["src"])) \
                or sha256_any(Path(c)) != t["sha256"] or sha256_any(Path(t["src"])) != t["sha256"]:
            return "surviving copy changed or missing"
    if t["reason"] == "installed" and not all(os.path.isdir(a) for a in t["app"].split(", ")):
        return "app no longer installed"
    return None


def classify(now: float, lines, stamp: str, by_size=None, apps_of=dmg_apps):
    """(trash rows, move candidates, skips) for every Desktop item."""
    trash, cands, skips = [], [], []
    tracked = rt.tracked_aliases(STATE_DIR / "desktop-aliases.json")
    try:
        entries = sorted(os.scandir(str(DESKTOP)), key=lambda e: e.name)
    except PermissionError:
        lines.append(f"{stamp} SKIP {DESKTOP} — permission denied (grant Desktop access to this python)")
        return None
    seen = {}
    for e in entries:
        name, p = e.name, Path(e.path)
        if ip.ignored(name) or name in DESKTOP_KEEP:
            continue
        if p.suffix.lower() in PARTIAL_EXTS:
            skips.append({"src": e.path, "reason": "partial-download"})
            continue
        try:
            st = os.lstat(e.path)
        except OSError:
            continue
        fp = fingerprint(st)
        if e.is_symlink():
            trash.append({"src": e.path, "reason": "symlink", "fp": fp})
            continue
        is_dir = e.is_dir(follow_symlinks=False)
        if not is_dir and (rt.alias_matches(e.path, tracked.get(e.path)) or (
                st.st_size < rt.ALIAS_MAX_BYTES and rt.finder_alias_flag(e.path))):
            trash.append({"src": e.path, "reason": "alias", "fp": fp,
                          "target": (tracked.get(e.path) or {}).get("target")})
            continue
        newest, has_git, incomplete = rt.walk_info(e.path, stop_after=now - rt.RECENT_S)
        if has_git or rt._source_problem(e.path) == "skipped-git-repo":
            lines.append(f"{stamp} WARN git repo on the Desktop left in place: {e.path}")
            skips.append({"src": e.path, "reason": "git-repo"})
            continue
        if incomplete:
            skips.append({"src": e.path, "reason": "scan-incomplete"})
            continue
        if newest > now - rt.RECENT_S:
            skips.append({"src": e.path, "reason": "recent"})
            continue
        if not is_dir:
            # empty files are never duplicates; nor is anything with resource-fork data
            digest = sha256_any(p) if st.st_size and not has_resource_fork(p) else None
            if digest and digest in seen:
                trash.append({"src": e.path, "reason": "duplicate", "copy_of": seen[digest],
                              "sha256": digest, "fp": fp})
                continue
            if digest and by_size is not None:
                copy = inbox_copy_of(p, st.st_size, digest, by_size)
                if copy:
                    trash.append({"src": e.path, "reason": "duplicate", "copy_of": copy,
                                  "sha256": digest, "fp": fp})
                    continue
            if digest:
                seen[digest] = e.path
            if p.suffix.lower() in INSTALLER_EXTS:
                app = installed_app_for(p, apps_of=apps_of)
                if app:
                    trash.append({"src": e.path, "reason": "installed", "app": app, "fp": fp})
                    continue
        cands.append({"op": "move", "src": e.path, "name": name, "kind": "dir" if is_dir else "file",
                      "type": route(p), "has_git": False, "mtime": st.st_mtime, "source": "desktop"})
    return trash, cands, skips


def existing_bundle(inbox: Path, tok: str):
    """The newest bundle folder this job made for a token (<type>/<project>/<month>/<date>-…-tok)."""
    found = [p for p in inbox.glob("*/*/*/20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*%s" % tok)
             if p.is_dir() and not p.is_symlink() and p.name.endswith("-" + tok)
             and rt._source_problem(str(p)) is None]  # never add into a repo
    return max(found, key=lambda p: (p.parts[-2], p.name)) if found else None


def plan_moves(cands, now: float, use_jev=True, evaluate=None, log=None):
    """Fill project, name and dst for each candidate; group shared-token items into bundles."""
    groups = {}
    for r in cands:
        tok = lead_token(r["name"])
        if tok:
            groups.setdefault(tok, []).append(r)
    inbox = ip.INBOX
    known = {t: existing_bundle(inbox, t) for t in groups}
    groups = {t: rs for t, rs in groups.items() if len(rs) >= GROUP_MIN or known[t]}
    rt.resolve_rows(cands, use_jev=use_jev, evaluate=evaluate, log=log)
    grouped = set()
    for tok, rs in groups.items():
        projects = {r["project"] for r in rs}
        project = projects.pop() if len(projects) == 1 else ip.UNASSIGNED
        types = {r["type"] for r in rs}
        btype = types.pop() if len(types) == 1 else "misc"
        day = max(item_date(r["name"], r["mtime"]) for r in rs)
        bundle = known[tok]  # a later run adds to the bundle an earlier run made
        if bundle:
            btype, project, month = bundle.parts[-4], bundle.parts[-3], bundle.parts[-2]
        else:
            month = day.strftime("%Y-%m")
            bundle = inbox / btype / project / month / clean_name(tok, project, day, True)
            n = 2  # the name is taken (e.g. by a bundle someone made a repo): use a fresh one
            while os.path.lexists(str(bundle)):
                bundle = bundle.with_name("%s-%02d" % (clean_name(tok, project, day, True), n))
                n += 1
        for r in rs:
            r.update(project=project, month=month, type=btype, bundle=str(bundle),
                     dst=str(bundle / r["name"]))
            grouped.add(r["src"])
    for r in cands:
        if r["src"] in grouped:
            continue
        day = item_date(r["name"], r["mtime"])
        r["month"] = day.strftime("%Y-%m")
        new = clean_name(r["name"], r["project"], day, r["kind"] == "dir")
        r["dst"] = str(inbox / r["type"] / r["project"] / r["month"] / new)
    for r in cands:  # a name clash gets -02, -03 ... (apply also refuses to overwrite)
        dst, n = Path(r["dst"]), 2
        while os.path.lexists(str(dst)) or any(o is not r and o.get("dst") == str(dst) for o in cands):
            base, ext = (r["dst"], "") if r["kind"] == "dir" else os.path.splitext(r["dst"])
            dst = Path("%s-%02d%s" % (base, n, ext))
            n += 1
        r["dst"] = str(dst)
    return cands


# --------------------------------------------------------------------- run
def run(dry=False, use_jev=True, now=None, evaluate=None, apps_of=dmg_apps, trasher=trash_path):
    now = now or time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines, summary = [], {}
    if not rt.is_canonical(DESKTOP):
        lines.append(f"{stamp} WARN {DESKTOP} is or sits behind a symlink; not cleaned")
        return lines, summary, 1
    by_size = inbox_size_index(ip.INBOX)
    plan = classify(now, lines, stamp, by_size=by_size, apps_of=apps_of)
    if plan is None:
        return lines, summary, 2
    trash, cands, skips = plan
    moves = plan_moves(cands, now, use_jev=use_jev, evaluate=evaluate,
                       log=lambda m: lines.append(f"{stamp} {m}"))
    manifest_path = MANIFEST_DIR / (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-desktop.jsonl")

    trashed = 0
    for t in trash:
        why = t["reason"] + (f" (copy at {t['copy_of']})" if t.get("copy_of") else "") + (
            f" (installed: {t['app']})" if t.get("app") else "")
        lines.append(f"{stamp} {'DRY ' if dry else ''}TRASH {t['src']} — {why}")
        if dry:
            continue
        err = still_disposable(t)
        if err:
            t["status"] = "skipped-" + err.replace(" ", "-")
            lines.append(f"{stamp} SKIP trash {t['src']} — {err}")
            continue
        t["trash_dst"], err = trasher(Path(t["src"]))
        t["status"] = "done" if err is None else err
        if err:
            lines.append(f"{stamp} WARN {err}: {t['src']}")
        else:
            trashed += 1
    if trash and not dry:  # aliases we tracked are gone now
        state = STATE_DIR / "desktop-aliases.json"
        try:
            kept = [e for e in json.loads(state.read_text()) if os.path.lexists(e.get("alias", ""))]
            state.write_text(json.dumps(kept, indent=1, sort_keys=True))
        except (OSError, ValueError):
            pass

    results = []
    if dry:
        for r in moves:
            lines.append(f"{stamp} DRY MOVE {r['src']} -> {r['dst']} ({r.get('basis')})")
    elif moves or trash:
        rt.write_manifest([{"op": "meta", "created": stamp, "kind": "desktop-clean",
                            "undo": f"org_router.py undo --manifest {manifest_path}"}]
                          + [dict(t, op="trash") for t in trash]
                          + [dict(s, op="skip") for s in skips] + moves, manifest_path)
        journal = rt.Journal(rt.applied_path(manifest_path))
        journal.append({"op": "meta", "applied": stamp, "source_manifest": str(manifest_path)})
        try:
            results = rt.apply_rows(moves, now=now, inbox=ip.INBOX, allow_src_roots=[DESKTOP],
                                    journal=journal, log=lambda m: lines.append(f"{stamp} {m}"))
        finally:
            journal.append({"op": "end"})
            journal.close()
    done = [r for r in results if r["status"] == "done"]
    for s in skips:
        lines.append(f"{stamp} SKIP {s['src']} — {s['reason']}")
    summary["trashed"] = trashed if not dry else len(trash)
    summary["moved"] = len(done) if not dry else len(moves)
    summary["left"] = len(skips) + len([r for r in results if r["status"] != "done"])
    summary["manifest"] = str(manifest_path) if (results or trashed) else "(none)"
    lines.append(f"{stamp} {'DRY ' if dry else ''}desktop-clean " + json.dumps(summary, sort_keys=True))
    failed = any(r["status"].startswith("failed") for r in results) or any(
        (t.get("status") or "").startswith("trash failed") for t in trash)
    return lines, summary, 1 if failed else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in argv if a not in ("--dry-run", "--no-jev")]
    if unknown:
        print("usage: desktop_clean.py [--dry-run] [--no-jev]", file=sys.stderr)
        return 2
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(STATE_DIR / "sweep.lock", "w")  # the same lock as inbox_sweep.py
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("desktop_clean: another run holds the sweep lock; exiting")
        return 0
    lines, _summary, rc = run(dry="--dry-run" in argv, use_jev="--no-jev" not in argv)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
