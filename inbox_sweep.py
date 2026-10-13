#!/usr/bin/env python3
"""inbox_sweep.py - daily Inbox/Desktop/Downloads routing engine (launchd com.zs.inbox-sweep, 07:30).

Replaces the weekly sweeper (2026-10-13 machine reorganisation, spec:
~/.config/zs-ops/org/EXECUTION-SPEC.md). One pass, one lock, one manifest:

  1. Expire Finder aliases this job left on the Desktop (3 days).
  2. Ingest loose files from ~/Desktop (not the Projects/Inbox aliases, not alias files) and
     ~/Downloads (older than 48h; never .crdownload/.download/.part) into the layout
     ~/Inbox/<type>/<project>/<YYYY-MM>/<name>. Files routed FROM the Desktop get a Finder
     alias on the Desktop.
  3. Route new top-level Inbox items (bundles move whole; nothing modified in the last 10
     minutes; handoffs only once older than 7 days).
  4. List >30-day unpinned items as cold-archive candidates (written, never archived here).
  5. Advance the screenshot location to this month's folder.
  6. Ingest fresh research notes (name contains "research", created in the last day) into
     NotebookLM.
  7. Write ~/Inbox/notes/mac-ops/<YYYY-MM>/<date>-mac-ops-report-cleanup.md and a JSONL
     manifest under ~/.config/zs-ops/org/manifests/ (undo: org_router.py undo --manifest F).

Never deletes, never touches repos, never overwrites. Mutations per run are capped
(ORG_SWEEP_MAX_MOVES, default 25) so an interrupted run stays easy to audit.

Usage: inbox_sweep.py [--dry-run] [--no-jev] [--no-ingest] [--no-screenshots] [--no-cold]
                      [--reap-archive]
Log:   ~/Library/Logs/zs-ai-os/inbox-sweep.log
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "org"))
import inbox_paths as ip  # noqa: E402
import org_router as rt  # noqa: E402


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


HOME = Path.home()
INBOX = ip.INBOX
DESKTOP = HOME / "Desktop"
DOWNLOADS = HOME / "Downloads"
ORG = HERE / "org"
STATE_DIR = ORG / "state"
MANIFEST_DIR = ORG / "manifests"
LOG = HOME / "Library/Logs/zs-ai-os/inbox-sweep.log"
DOWNLOADS_MIN_AGE_S = 48 * 3600
ALIAS_TTL_S = 3 * 86400
COLD_AGE_S = 30 * 86400
MAX_MOVES = _env_int("ORG_SWEEP_MAX_MOVES", 25)
INBOX_INGEST_CAP = _env_int("INBOX_INGEST_CAP", 15)
ARCHIVE_TTL_DAYS = _env_int("INBOX_ARCHIVE_TTL_DAYS", 90)
DELETIONS = INBOX / "deletions"
STAGED_AT = ".staged-at"
NOTEBOOKLM = HOME / "bin/notebooklm"
NOTEBOOKLM_TIMEOUT_S = 120
MARKITDOWN = HOME / ".local/bin/markitdown"  # NotebookLM rejects raw .html uploads; convert to .md first
DESKTOP_KEEP = {"Projects", "Inbox"}
PARTIAL_EXTS = {".crdownload", ".download", ".part", ".partial", ".tmp"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SCREENSHOT_RE = re.compile(r"(screenshot|screen shot|qa-|_preview)", re.IGNORECASE)
OPERATIONAL_REPORT_RE = re.compile(
    r"(os-doctor-report|dream-digest|machine-map|agent-prune-manifest|"
    r"reconciliation-manifest|mem0-payloads|handoff)",
    re.IGNORECASE,
)
ROUTES = {".html": "html", ".md": "notes", ".pdf": "pdfs",
          ".json": "misc", ".csv": "misc", ".txt": "misc"}
INGEST_EXTS = {".md", ".html"}

# HOLD guard (2026-08-01). Anything matching these is never treated as a cold candidate; it
# is listed as HELD so it gets filed into a project tree. Bias is deliberately toward
# over-holding: a false positive leaves one extra file in the Inbox, a false negative loses
# something irreplaceable. Separator is [\s_-]* on every multi-word term: real filenames mix
# "Pitch Deck", "pitch-deck" and "pitch_deck" freely, and a PDF's name is its ONLY signal.
PROTECT_NAME_RE = re.compile(
    r"(confidential|operating[\s_-]*model|source[\s_-]*of[\s_-]*truth|"
    r"term[\s_-]*sheet|cap[\s_-]*table|pitch[\s_-]*deck|"
    r"contract|agreement|\bloi\b|invoice|decision[\s_-]*log|\bnda\b|\blegal\b|privileged|restricted|\bhealth\b)",
    re.IGNORECASE,
)
PROTECT_CONTENT_RE = re.compile(
    r"(confidential|source of truth|internal working document|"
    r"do not distribute|proprietary and)",
    re.IGNORECASE,
)
PROTECT_SCAN_EXTS = {".md", ".txt", ".html", ".csv"}
PROTECT_SCAN_BYTES = 4096


def configure(home: Path = None, inbox: Path = None) -> None:
    """Point every path at a different home/inbox (tests). Keeps org_router in sync."""
    global HOME, INBOX, DESKTOP, DOWNLOADS, DELETIONS, LOG, NOTEBOOKLM, MARKITDOWN
    if home is not None:
        HOME = Path(home)
        DESKTOP = HOME / "Desktop"
        DOWNLOADS = HOME / "Downloads"
        LOG = HOME / "Library/Logs/zs-ai-os/inbox-sweep.log"
        NOTEBOOKLM = HOME / "bin/notebooklm"
        MARKITDOWN = HOME / ".local/bin/markitdown"
    if inbox is not None:
        INBOX = Path(inbox)
    elif home is not None:
        INBOX = HOME / "Inbox"
    DELETIONS = INBOX / "deletions"
    ip.INBOX = INBOX


def route(path: Path):
    """Inbox type for a loose file by extension, or None (never moved)."""
    ext = path.suffix.lower()
    if ext in ROUTES:
        return ROUTES[ext]
    if ext in IMAGE_EXTS:
        return "screenshots" if SCREENSHOT_RE.search(path.name) else "misc"
    return None


def _with_collision_suffix(dest: Path) -> Path:
    if not dest.exists():
        return dest
    suffix = time.strftime("-%Y%m%d-%H%M%S")
    return dest.with_name(dest.stem + suffix + dest.suffix)


def _safe_listdir(d: Path):
    """List a directory, tolerating macOS TCC/permission denials.

    Returns (entries, warning). A protected dir (no Full Disk Access) is
    skipped with a warning instead of crashing the whole sweep.
    """
    try:
        return list(d.iterdir()), None
    except PermissionError:
        return [], f"SKIP {d} — permission denied (grant Full Disk Access to the sweep's python)"
    except OSError as exc:
        return [], f"SKIP {d} — {exc}"


def _protected_reason(path: Path):
    """Why this file must not be listed as a cold candidate, or None."""
    if PROTECT_NAME_RE.search(path.name):
        return "name"
    if path.suffix.lower() not in PROTECT_SCAN_EXTS or not path.is_file():
        return None
    try:
        with path.open("r", errors="replace") as fh:
            head = fh.read(PROTECT_SCAN_BYTES)
    except OSError:
        return None
    return "content" if PROTECT_CONTENT_RE.search(head) else None


def _move_file(src: Path, dest: Path):
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
    except OSError as exc:
        return str(exc)
    return None


def _prepare_log(lines, stamp: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        lines.append(f"{stamp} WARN log directory unavailable {LOG.parent}: {exc}")


def _write_log(lines, stamp: str) -> None:
    try:
        with LOG.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        lines.append(f"{stamp} WARN log write failed {LOG}: {exc}")


# --------------------------------------------------------------- NotebookLM
def _html_as_markdown(path: Path):
    """Convert an .html file to a temp .md for upload. Returns (md_path, error)."""
    if not MARKITDOWN.is_file():
        return None, f"markitdown missing at {MARKITDOWN}"
    md_path = path.with_suffix(".zs-ingest.md")
    try:
        proc = subprocess.run(
            [str(MARKITDOWN), str(path)],
            stdout=md_path.open("w"),
            stderr=subprocess.PIPE,
            text=True,
            timeout=NOTEBOOKLM_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        md_path.unlink(missing_ok=True)
        return None, f"markitdown: {exc}"
    if proc.returncode != 0 or not md_path.stat().st_size:
        md_path.unlink(missing_ok=True)
        return None, f"markitdown rc={proc.returncode}"
    return md_path, None


def _ingest_source(path: Path):
    upload = path
    temp_md = None
    if path.suffix.lower() == ".html":
        temp_md, err = _html_as_markdown(path)
        if err:
            return err
        upload = temp_md
    cmd = [str(NOTEBOOKLM), "--quiet", "source", "add", "--type", "file",
           "--title", path.stem, str(upload)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True, timeout=NOTEBOOKLM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"timeout after {NOTEBOOKLM_TIMEOUT_S}s"
    except OSError as exc:
        return str(exc)
    finally:
        if temp_md is not None:
            temp_md.unlink(missing_ok=True)
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or "").strip().splitlines()
    tail = f": {detail[-1]}" if detail else ""
    return f"rc={proc.returncode}{tail}"


# ------------------------------------------------------------------- state
def _state_path(name: str) -> Path:
    return STATE_DIR / name


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, str(path))


# ------------------------------------------------------- Desktop aliases
def finder_alias_flag(path: Path) -> bool:
    """True when com.apple.FinderInfo marks the file as a Finder alias (flag 0x8000)."""
    return rt.finder_alias_flag(path)


def is_alias_file(path: Path, tracked=None) -> bool:
    """Alias to skip when ingesting Desktop files: one we created (same path AND same inode, so
    a real file that later reuses the name is not mistaken for it), or flagged in Finder info."""
    if tracked and str(path) in tracked:
        try:
            if tracked[str(path)] in (None, os.lstat(str(path)).st_ino):
                return True
        except OSError:
            return False
    return finder_alias_flag(path)


def make_finder_alias(target: Path, folder: Path, name: str):
    """Create a Finder alias to target inside folder. Returns an error string or None."""
    script = (
        'on run argv\n'
        'tell application "Finder"\n'
        'make new alias file to (POSIX file (item 1 of argv) as alias) at '
        '(POSIX file (item 2 of argv) as alias) with properties {name:(item 3 of argv)}\n'
        'end tell\nend run\n')
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script, str(target), str(folder), name],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return type(exc).__name__
    if out.returncode != 0:
        return (out.stderr or "osascript failed").strip().splitlines()[-1][:160]
    return None


def expire_desktop_aliases(now: float, lines, stamp: str, dry: bool, alias_state=None):
    """Remove tracked aliases older than 3 days. An alias is removed only when it is still
    verifiably ours: same inode and sha256 as recorded at creation, a small regular file, and
    still flagged as a Finder alias. Anything else is left on disk, logged, and untracked."""
    path = _state_path("desktop-aliases.json")
    entries = alias_state if alias_state is not None else _load_json(path, [])
    keep, removed = [], 0
    for e in entries:
        alias = Path(e["alias"])
        if now - e.get("created", now) < ALIAS_TTL_S:
            keep.append(e)
            continue
        if not os.path.lexists(str(alias)):
            lines.append(f"{stamp} alias already gone {alias}")
            continue
        if not rt.alias_matches(str(alias), e):
            lines.append(f"{stamp} WARN alias {alias} no longer matches what was created "
                         "(inode, sha256 or alias flag); left in place and untracked")
            continue
        lines.append(f"{stamp} {'DRY ' if dry else ''}EXPIRE alias {alias} (target {e.get('target')})")
        if dry:
            keep.append(e)
            continue
        try:
            os.remove(str(alias))
            removed += 1
        except OSError as exc:
            lines.append(f"{stamp} WARN alias removal failed {alias}: {exc}")
            keep.append(e)
    if not dry and alias_state is None:
        _save_json(path, keep)
    return keep, removed


def record_alias(entries, alias: Path, target: str, now: float):
    """Track a created alias with its inode and sha256 (needed before it may be removed)."""
    try:
        ino = os.lstat(str(alias)).st_ino
    except OSError:
        ino = None
    entries[:] = [e for e in entries if e["alias"] != str(alias)]
    entries.append({"alias": str(alias), "target": target, "created": now, "ino": ino,
                    "sha256": rt.sha256_file(str(alias))})


# ----------------------------------------------------------- external src
def external_rows(now: float, lines, stamp: str, alias_entries):
    """Move rows for loose Desktop and Downloads files (resolve_rows fills project/dst)."""
    rows, warnings = [], []
    tracked = {e["alias"]: e.get("ino") for e in alias_entries}
    for source, root, min_age in (("desktop", DESKTOP, 0), ("downloads", DOWNLOADS, DOWNLOADS_MIN_AGE_S)):
        if not root.is_dir():
            continue
        if not rt.is_canonical(root):
            lines.append(f"{stamp} WARN {root} is or sits behind a symlink; not scanned")
            continue
        entries, warn = _safe_listdir(root)
        if warn:
            warnings.append(warn)
            lines.append(f"{stamp} {warn}")
        for p in sorted(entries, key=lambda q: q.name):
            name = p.name
            if ip.ignored(name) or (source == "desktop" and name in DESKTOP_KEEP):
                continue
            if p.is_symlink() or not p.is_file():
                continue
            if p.suffix.lower() in PARTIAL_EXTS:
                continue
            dest_type = route(p)
            if dest_type is None:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if now - mtime < max(min_age, rt.RECENT_S):
                continue
            if source == "desktop" and is_alias_file(p, tracked):
                continue
            rows.append({"op": "move", "src": str(p), "type": dest_type, "name": name, "kind": "file",
                         "has_git": False, "mtime": mtime, "source": source})
    return rows, warnings


def _uniquify_dst(row: dict) -> None:
    """A name clash gets a -YYYYMMDD-HHMMSS suffix (the apply step also refuses to overwrite)."""
    dst = Path(row["dst"])
    if os.path.lexists(str(dst)):
        row["dst"] = str(_with_collision_suffix(dst))
        row["renamed_on_collision"] = True


# -------------------------------------------------------- cold candidates
def _pinned_set():
    names = set()
    try:
        for line in (INBOX / ".pinned").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.add(line)
    except OSError:
        pass
    return names


def cold_candidates(now: float):
    """(candidates, held) of items older than 30 days by newest mtime inside the item."""
    pinned = _pinned_set()
    cands, held = [], []
    latest_handoff = {}
    scan = []
    for t in ip.inbox_types():
        scan.append((t, INBOX / t))
    scan.append(("handoffs", INBOX / "notes" / "handoffs"))
    for tname, tdir in scan:
        if not tdir.is_dir():
            continue
        for item in ip.find_items(type=tname, limit=0):
            p = Path(item["path"])
            newest, has_git, trunc = rt.walk_info(p)
            item = dict(item, newest=newest, has_git=has_git)
            if tname == "handoffs":
                cur = latest_handoff.get(item["project"])
                if cur is None or newest > cur[0]:
                    latest_handoff[item["project"]] = (newest, item["path"])
            if now - newest < COLD_AGE_S:
                continue
            rel = rt._rel(item["path"])
            reason = None
            if rel in pinned or item["name"] in pinned or item["path"] in pinned:
                reason = "pinned"
            elif (p / ".pinned").exists():
                reason = "pinned"
            elif _protected_reason(p):
                reason = "protected-name-or-content"
            elif has_git:
                reason = "contains-git"
            (held if reason else cands).append(dict(item, reason=reason))
    keepers = {path for _t, path in latest_handoff.values()}
    still = []
    for c in cands:
        if c["type"] == "handoffs" and c["path"] in keepers:
            held.append(dict(c, reason="latest-handoff-for-project"))
        else:
            still.append(c)
    return still, held


def write_cold_candidates(cands, held, now: float, dry: bool):
    day = _dt.datetime.fromtimestamp(now)
    body = ["# Mac ops cleanup candidates (cold-archive queue)", "",
            f"Generated {day.isoformat(timespec='seconds')}. Items whose newest file is older than 30 days "
            "and that are not pinned or held. Nothing here is archived or removed by this job: a later "
            "verified step (tar.zst on the backup drive, list and hash checked) consumes the queue.", "",
            f"- Candidates: {len(cands)}", f"- Held (pinned, protected, git, latest handoff): {len(held)}", ""]
    if cands:
        body += ["## Candidates", "", "| Age (days) | Type | Project | Item | Note |", "|---|---|---|---|---|"]
        for c in sorted(cands, key=lambda c: c["newest"]):
            note = "plan: age alone does not complete a plan" if c["type"] == "plans" else ""
            body.append("| %d | %s | %s | `%s` | %s |" % ((now - c["newest"]) / 86400, c["type"],
                                                         c["project"], c["path"], note))
    if held:
        body += ["", "## Held", "", "| Type | Project | Item | Why |", "|---|---|---|---|"]
        for c in held:
            body.append("| %s | %s | `%s` | %s |" % (c["type"], c["project"], c["path"], c["reason"]))
    path = INBOX / "notes" / "mac-ops" / day.strftime("%Y-%m") / (
        day.strftime("%Y-%m-%d") + "-mac-ops-cleanup-candidates.md")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(body) + "\n")
        _save_json(_state_path("cold-queue.json"),
                   {"generated": day.isoformat(timespec="seconds"),
                    "candidates": [{"path": c["path"], "type": c["type"], "project": c["project"],
                                    "newest": c["newest"]} for c in cands]})
    return path


# ------------------------------------------------------------- screenshots
def advance_screenshot_location(now: float, lines, stamp: str, dry: bool, runner=subprocess.run):
    """Point macOS screenshots at <Inbox>/screenshots/_unassigned/<this month>.

    Only moves the setting when it already points into that tree (or is unset); a location
    the user chose elsewhere is left alone. The previous month folder is never removed.
    """
    day = _dt.datetime.fromtimestamp(now)
    cur_month = day.strftime("%Y-%m")
    nxt = (day.replace(day=1) + _dt.timedelta(days=32)).strftime("%Y-%m")
    base = INBOX / "screenshots" / ip.UNASSIGNED
    target = base / cur_month
    try:
        out = runner(["/usr/bin/defaults", "read", "com.apple.screencapture", "location"],
                     capture_output=True, text=True, timeout=10)
        current = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        current = ""
    managed = (not current) or (current.startswith(str(base) + "/")
                                and re.fullmatch(r"\d{4}-\d{2}", Path(current).name) is not None)
    if not managed:
        lines.append(f"{stamp} screenshot location left alone ({current})")
        return False
    if current == str(target):
        if not dry:
            (base / nxt).mkdir(parents=True, exist_ok=True)
        return False
    lines.append(f"{stamp} {'DRY ' if dry else ''}screenshot location {current or '(unset)'} -> {target}")
    if dry:
        return True
    target.mkdir(parents=True, exist_ok=True)
    (base / nxt).mkdir(parents=True, exist_ok=True)
    try:
        runner(["/usr/bin/defaults", "write", "com.apple.screencapture", "location", str(target)],
               capture_output=True, timeout=10)
        runner(["/usr/bin/killall", "SystemUIServer"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"{stamp} WARN screenshot location write failed: {type(exc).__name__}")
        return False
    return True


# ---------------------------------------------------------------- ingest
def fresh_research_files(now: float):
    """Research notes created in the last day (birth time), .md/.html, not operational reports."""
    out = []
    for tname in ("notes", "html"):
        for item in ip.find_items(type=tname, query="research", limit=0):
            p = Path(item["path"])
            if item["is_dir"] or p.suffix.lower() not in INGEST_EXTS:
                continue
            if "research" not in p.name.lower() or OPERATIONAL_REPORT_RE.search(p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            born = getattr(st, "st_birthtime", st.st_mtime)
            if now - born <= 86400:
                out.append((p, st.st_size))
    return out


def ingest_research(now: float, lines, stamp: str, dry: bool, ingest=None):
    files = fresh_research_files(now)
    ledger_path = _state_path("ingested.json")
    done = set(_load_json(ledger_path, []))
    todo = [(p, sz) for p, sz in files if f"{p.name}:{sz}" not in done]
    if not todo:
        return 0, 0
    if not dry and ingest is None and not NOTEBOOKLM.is_file():
        lines.append(f"{stamp} WARN NotebookLM binary missing at {NOTEBOOKLM}; {len(todo)} research note(s) not ingested")
        return 0, len(todo)
    ingest = ingest or _ingest_source
    ok = failed = 0
    for p, sz in todo[:INBOX_INGEST_CAP]:
        lines.append(f"{stamp} {'DRY ' if dry else ''}INGEST {p} -> NotebookLM title=\"{p.stem}\"")
        if dry:
            ok += 1
            continue
        err = ingest(p)
        if err:
            failed += 1
            lines.append(f"{stamp} WARN ingest failed {p}: {err}")
        else:
            ok += 1
            done.add(f"{p.name}:{sz}")
    if len(todo) > INBOX_INGEST_CAP:
        lines.append(f"{stamp} SKIP ingest cap: {len(todo) - INBOX_INGEST_CAP} research note(s) left for tomorrow")
    if not dry:
        _save_json(ledger_path, sorted(done))
    return ok, failed


# ---------------------------------------------------------------- archive
def _month_end_epoch(month: str):
    """Epoch seconds for the first instant AFTER the given YYYY-MM month."""
    try:
        year, mon = (int(part) for part in month.split("-"))
        nxt = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    except (ValueError, TypeError):
        return None
    return time.mktime(nxt.timetuple())


def reap_archive(lines, stamp: str, dry: bool) -> int:
    """Stage _archive/<YYYY-MM>/ folders past the TTL into deletions/ (opt-in: --reap-archive).

    Staging is NOT deletion. The month folder's NAME is the clock, deliberately not its
    mtime: archiving is a rename, which preserves each file's original mtime, so reaping by
    mtime would stage a just-archived file the moment it landed.
    """
    archive_root = INBOX / "_archive"
    if not archive_root.is_dir():
        return 0
    entries, warn = _safe_listdir(archive_root)
    if warn:
        lines.append(f"{stamp} {warn}")
        return 0

    now = time.time()
    reaped = 0
    dest_root = DELETIONS / f"{time.strftime('%Y-%m-%d')}-archive-expiry"
    for month_dir in sorted(entries):
        if not month_dir.is_dir():
            continue
        month_end = _month_end_epoch(month_dir.name)
        if month_end is None:
            lines.append(f"{stamp} SKIP reap {month_dir} — unrecognized month folder name")
            continue
        age_days = (now - month_end) / 86400
        if age_days <= ARCHIVE_TTL_DAYS:
            continue
        dest = _with_collision_suffix(dest_root / month_dir.name)
        lines.append(
            f"{stamp} {'DRY ' if dry else ''}REAP {month_dir} -> {dest} "
            f"({age_days:.0f}d past month end, TTL {ARCHIVE_TTL_DAYS}d)"
        )
        if dry:
            reaped += 1
            continue
        err = _move_file(month_dir, dest)
        if err:
            lines.append(f"{stamp} WARN reap failed {month_dir}: {err}")
            continue
        try:
            (dest / STAGED_AT).write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} inbox_sweep reap\n")
        except OSError as exc:
            lines.append(f"{stamp} WARN could not write {STAGED_AT} in {dest}: {exc}")
        reaped += 1
    return reaped


# ------------------------------------------------------------------ report
def write_report(now: float, summary: dict, lines_for_report, dry: bool):
    day = _dt.datetime.fromtimestamp(now)
    path = INBOX / "notes" / "mac-ops" / day.strftime("%Y-%m") / (
        day.strftime("%Y-%m-%d") + "-mac-ops-report-cleanup.md")
    sec = ["", f"## Run {day.strftime('%H:%M')}", ""]
    sec += [f"- {k}: {v}" for k, v in summary.items()]
    sec += [""] + lines_for_report
    if dry:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "" if path.exists() else "# Mac ops report: cleanup (%s)\n" % day.strftime("%Y-%m-%d")
    with path.open("a") as fh:
        fh.write(head + "\n".join(sec) + "\n")
    return path


# -------------------------------------------------------------------- main
def run(dry=False, use_jev=True, do_ingest=True, do_screens=True, do_cold=True, do_reap=False,
        now=None, evaluate=None, alias_maker=None, ingest=None, runner=subprocess.run):
    now = now or time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = []
    _prepare_log(lines, stamp)
    summary = {}
    warnings = []
    alias_maker = alias_maker or make_finder_alias

    if not dry:
        recon = rt.reconcile_all(MANIFEST_DIR, log=lambda m: lines.append(f"{stamp} WARN {m}"))
        if recon:
            warnings.append(f"reconciled {recon} interrupted move(s)")
    entries, expired = expire_desktop_aliases(now, lines, stamp, dry)
    summary["desktop aliases expired"] = expired

    ext, warn = external_rows(now, lines, stamp, entries)
    warnings += warn
    inbox_rows, counts = rt.collect_inbox_candidates(now)
    rows = ext + [r for r in inbox_rows]
    skips = [r for r in rows if r["op"] == "skip"]
    rt.resolve_rows(rows, use_jev=use_jev, evaluate=evaluate, log=lambda m: lines.append(f"{stamp} {m}"))
    for r in rows:
        if r["op"] == "move":
            _uniquify_dst(r)
    moves = [r for r in rows if r["op"] == "move"]

    manifest_path = MANIFEST_DIR / (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-sweep.jsonl")
    if dry:
        for r in moves:
            lines.append(f"{stamp} DRY {r['src']} -> {r['dst']} ({r['basis']})")
        results = []
    else:
        # plan first, then a journal: intent row before each rename, result row after it
        rt.write_manifest([{"op": "meta", "created": stamp, "kind": "daily-sweep",
                            "undo": f"org_router.py undo --manifest {manifest_path}"}] + skips + moves,
                          manifest_path)
        journal = rt.Journal(rt.applied_path(manifest_path))
        journal.append({"op": "meta", "applied": stamp, "source_manifest": str(manifest_path)})
        try:
            results = rt.apply_rows(moves, max_moves=MAX_MOVES, now=now, inbox=INBOX,
                                    allow_src_roots=[DESKTOP, DOWNLOADS], journal=journal,
                                    log=lambda m: lines.append(f"{stamp} {m}"))
        finally:
            journal.append({"op": "end"})
            journal.close()
    done = [r for r in results if r["status"] == "done"]
    # Finder alias for items routed from the Desktop
    made = 0
    for r in done:
        if r.get("source") != "desktop":
            continue
        err = alias_maker(Path(r["dst"]), DESKTOP, r["name"])
        if err:
            lines.append(f"{stamp} WARN desktop alias for {r['name']} failed: {err}")
            continue
        record_alias(entries, DESKTOP / r["name"], r["dst"], now)
        made += 1
    if not dry:
        _save_json(_state_path("desktop-aliases.json"), entries)
    by_type, by_proj = {}, {}
    for r in done:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        by_proj[r["project"]] = by_proj.get(r["project"], 0) + 1
    summary["items planned"] = len(moves)
    summary["items moved"] = len(done)
    summary["moved by type"] = json.dumps(by_type, sort_keys=True)
    summary["moved by project"] = json.dumps(by_proj, sort_keys=True)
    summary["unassigned moved"] = by_proj.get(ip.UNASSIGNED, 0)
    summary["desktop aliases created"] = made
    reasons = {}
    for r in skips + [x for x in results if x["status"] != "done"]:
        key = r.get("reason") or r.get("status")
        reasons[key] = reasons.get(key, 0) + 1
    summary["left in place (reason: count)"] = json.dumps(reasons, sort_keys=True)
    if not moves and not warnings:
        lines.append(f"{stamp} {'DRY ' if dry else ''}nothing to route")

    if do_cold:
        cands, held = cold_candidates(now)
        cpath = write_cold_candidates(cands, held, now, dry)
        summary["cold-archive candidates"] = len(cands)
        summary["held (pinned/protected)"] = len(held)
        lines.append(f"{stamp} {'DRY ' if dry else ''}cold candidates: {len(cands)} held: {len(held)} -> {cpath}")
    if do_screens:
        changed = advance_screenshot_location(now, lines, stamp, dry, runner=runner)
        summary["screenshot location advanced"] = changed
    if do_ingest:
        ok, failed = ingest_research(now, lines, stamp, dry, ingest=ingest)
        summary["research notes ingested"] = f"{ok} ok, {failed} failed"
    if do_reap:
        summary["archive months staged"] = reap_archive(lines, stamp, dry)
    summary["manifest"] = str(manifest_path) if not dry else "(dry run)"
    summary["undo"] = f"python3 {HERE}/org/org_router.py undo --manifest {manifest_path}" if not dry else "n/a"

    report_lines = [f"- {l[len(stamp) + 1:]}" for l in lines if "WARN" in l or "SKIP" in l]
    rpt = write_report(now, summary, report_lines, dry)
    lines.append(f"{stamp} {'DRY ' if dry else ''}report -> {rpt}")
    if not dry:
        try:
            import inbox_index  # noqa: E402  (org/ is on sys.path)
            lines.append(f"{stamp} index -> {inbox_index.write_index()}")
        except Exception as exc:  # the index is a convenience; never fail the sweep over it
            warnings.append(f"index not written: {type(exc).__name__}")
    return lines, warnings, summary


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(STATE_DIR / "sweep.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("inbox_sweep: another run holds the lock; exiting")
        return 0
    lock = contextlib.nullcontext()
    if not dry:  # one mover at a time: the verified-archive job holds the same lock
        try:
            import org_archive  # noqa: E402
            lock = org_archive.archive_lock()
        except Exception:
            pass
    with lock:
        lines, warnings, _summary = run(
            dry=dry, use_jev="--no-jev" not in argv, do_ingest="--no-ingest" not in argv,
            do_screens="--no-screenshots" not in argv, do_cold="--no-cold" not in argv,
            do_reap="--reap-archive" in argv)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_log(lines, stamp)
    print("\n".join(lines))
    if any("permission denied" in w for w in warnings):
        return 2  # surface TCC denial to launchd so the scheduler watchdog alerts
    return 0


if __name__ == "__main__":
    sys.exit(main())
