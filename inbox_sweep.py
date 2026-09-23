#!/usr/bin/env python3
"""inbox_sweep.py — weekly sweep of stray AI deliverables into ~/Inbox/<type>/.

Conservative by design (reorg plan 2026-06-10, ~/.claude/plans/i-would-like-to-wise-breeze.md):
  - Sources: ~/Desktop (loose files), ~/Downloads (files older than 48h),
    ~/Documents/Codex/* (Codex session leftovers, any age).
  - Only files whose extension is on the allowlist move; everything else stays.
    Finder aliases (no extension), dirs, apps, and personal media (HEIC/RAW/video/audio)
    are never touched.
  - Routing: .html→html · .md→notes · .pdf→pdfs · screenshot-named images→screenshots,
    other images→misc · .json/.csv/.txt→misc.
  - Collisions get a -YYYYMMDD-HHMMSS suffix. Every move is logged.
  - HOLD guard (2026-08-01): files that look client-critical or confidential are never
    aged out of the Inbox; they are logged as HOLD so they get filed into a project tree.
  - Archive TTL (2026-08-01): _archive/<YYYY-MM>/ folders older than the TTL are staged
    into deletions/ — the sweep's terminal stage. Staging is never deletion.

Usage: inbox_sweep.py [--dry-run] [--no-expire] [--no-reap]
Log:   ~/Library/Logs/zs-ai-os/inbox-sweep.log
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


HOME = Path.home()
INBOX = HOME / "Inbox"
LOG = HOME / "Library/Logs/zs-ai-os/inbox-sweep.log"
DOWNLOADS_MIN_AGE_S = 48 * 3600
EXPIRE_DAYS = _env_int("INBOX_EXPIRE_DAYS", 14)
INBOX_INGEST_CAP = _env_int("INBOX_INGEST_CAP", 15)
ARCHIVE_TTL_DAYS = _env_int("INBOX_ARCHIVE_TTL_DAYS", 90)
DELETIONS = INBOX / "deletions"
STAGED_AT = ".staged-at"
NOTEBOOKLM = HOME / "bin/notebooklm"
NOTEBOOKLM_TIMEOUT_S = 120
MARKITDOWN = HOME / ".local/bin/markitdown"  # NotebookLM rejects raw .html uploads; convert to .md first

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SCREENSHOT_RE = re.compile(r"(screenshot|screen shot|qa-|_preview)", re.IGNORECASE)
OPERATIONAL_REPORT_RE = re.compile(
    r"(os-doctor-report|dream-digest|machine-map|agent-prune-manifest|"
    r"reconciliation-manifest|mem0-payloads|handoff)",
    re.IGNORECASE,
)
ROUTES = {".html": "html", ".md": "notes", ".pdf": "pdfs",
          ".json": "misc", ".csv": "misc", ".txt": "misc"}
EXPIRE_SUBDIRS = ("html", "notes", "pdfs", "misc", "screenshots")
INGEST_SUBDIRS = {"html", "notes"}
INGEST_EXTS = {".md", ".html"}

# HOLD guard (2026-08-01). Expiry ages files out by mtime alone, which treats a
# confidential client operating model exactly like a dream-digest. On 2026-08-01 that
# had buried rise-performance-operating-model-v8-master.md — the declared single source
# of truth for a live client, with no copy anywhere else on disk — in _archive/2026-07/.
# Anything matching these is never expired; it is logged as HOLD so it gets filed into
# a project tree instead. Bias is deliberately toward over-holding: a false positive
# leaves one extra file in the Inbox, a false negative loses something irreplaceable.
# Separator is [\s_-]* on every multi-word term: real filenames mix "Pitch Deck",
# "pitch-deck" and "pitch_deck" freely, and a PDF's name is its ONLY signal because
# PROTECT_SCAN_EXTS cannot read inside it.
PROTECT_NAME_RE = re.compile(
    r"(confidential|operating[\s_-]*model|source[\s_-]*of[\s_-]*truth|"
    r"term[\s_-]*sheet|cap[\s_-]*table|pitch[\s_-]*deck|"
    r"contract|agreement|\bloi\b|invoice|decision[\s_-]*log)",
    re.IGNORECASE,
)
PROTECT_CONTENT_RE = re.compile(
    r"(confidential|source of truth|internal working document|"
    r"do not distribute|proprietary and)",
    re.IGNORECASE,
)
PROTECT_SCAN_EXTS = {".md", ".txt", ".html", ".csv"}
PROTECT_SCAN_BYTES = 4096


def route(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in ROUTES:
        return ROUTES[ext]
    if ext in IMAGE_EXTS:
        return "screenshots" if SCREENSHOT_RE.search(path.name) else "misc"
    return None  # not on allowlist — never moved


def _with_collision_suffix(dest: Path) -> Path:
    if not dest.exists():
        return dest
    suffix = time.strftime("-%Y%m%d-%H%M%S")
    return dest.with_name(dest.stem + suffix + dest.suffix)


def _safe_listdir(d: Path) -> tuple[list[Path], str | None]:
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


def candidates() -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    warnings: list[str] = []
    desktop = HOME / "Desktop"
    if desktop.is_dir():
        entries, warn = _safe_listdir(desktop)
        if warn:
            warnings.append(warn)
        found += [p for p in entries if p.is_file() and not p.name.startswith(".")]
    downloads = HOME / "Downloads"
    if downloads.is_dir():
        now = time.time()
        entries, warn = _safe_listdir(downloads)
        if warn:
            warnings.append(warn)
        found += [p for p in entries
                  if p.is_file() and not p.name.startswith(".")
                  and now - p.stat().st_mtime > DOWNLOADS_MIN_AGE_S]
    codex = HOME / "Documents/Codex"
    if codex.is_dir():
        try:
            found += [p for p in codex.rglob("*") if p.is_file() and not p.name.startswith(".")]
        except PermissionError:
            warnings.append(f"SKIP {codex} — permission denied (grant Full Disk Access to the sweep's python)")
        except OSError as exc:
            warnings.append(f"SKIP {codex} — {exc}")
    return found, warnings


def _expired_files() -> tuple[list[tuple[Path, str, float]], list[str], list[tuple[Path, str]]]:
    found: list[tuple[Path, str, float]] = []
    warnings: list[str] = []
    held: list[tuple[Path, str]] = []
    cutoff = time.time() - (EXPIRE_DAYS * 86400)
    for subdir in EXPIRE_SUBDIRS:
        root = INBOX / subdir
        if not root.is_dir():
            continue
        try:
            entries = list(root.rglob("*"))
        except PermissionError:
            warnings.append(f"SKIP {root} — permission denied (grant Full Disk Access to the sweep's python)")
            continue
        except OSError as exc:
            warnings.append(f"SKIP {root} — {exc}")
            continue
        for path in entries:
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                warnings.append(f"SKIP {path} — {exc}")
                continue
            if mtime < cutoff:
                reason = _protected_reason(path)
                if reason:
                    held.append((path, reason))
                    continue
                found.append((path, subdir, mtime))
    found.sort(key=lambda item: (item[2], item[0].name.lower()))
    held.sort(key=lambda item: item[0].name.lower())
    return found, warnings, held


def _protected_reason(path: Path) -> str | None:
    """Why this file must not be aged out of the Inbox, or None.

    Checks the filename first (cheap, works for PDFs and images), then the head of
    text-ish files — the strongest signal is usually a confidentiality banner in the
    document itself rather than its name.
    """
    if PROTECT_NAME_RE.search(path.name):
        return "name"
    if path.suffix.lower() not in PROTECT_SCAN_EXTS:
        return None
    try:
        with path.open("r", errors="replace") as fh:
            head = fh.read(PROTECT_SCAN_BYTES)
    except OSError:
        return None  # unreadable: leave it to the normal expiry path
    return "content" if PROTECT_CONTENT_RE.search(head) else None


def _needs_ingest(path: Path, subdir: str) -> bool:
    return (
        subdir in INGEST_SUBDIRS
        and path.suffix.lower() in INGEST_EXTS
        and not OPERATIONAL_REPORT_RE.search(path.name)
    )


def _archive_dest(path: Path, subdir: str, month: str) -> Path:
    return _with_collision_suffix(INBOX / "_archive" / month / subdir / path.name)


def _move_file(src: Path, dest: Path) -> str | None:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
    except OSError as exc:
        return str(exc)
    return None


def _prepare_log(lines: list[str], stamp: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        lines.append(f"{stamp} WARN log directory unavailable {LOG.parent}: {exc}")


def _write_log(lines: list[str], stamp: str) -> None:
    try:
        with LOG.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        lines.append(f"{stamp} WARN log write failed {LOG}: {exc}")


def _html_as_markdown(path: Path) -> tuple[Path | None, str | None]:
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


def _ingest_source(path: Path) -> str | None:
    upload = path
    temp_md: Path | None = None
    if path.suffix.lower() == ".html":
        temp_md, err = _html_as_markdown(path)
        if err:
            return err
        upload = temp_md
    cmd = [
        str(NOTEBOOKLM),
        "--quiet",
        "source",
        "add",
        "--type",
        "file",
        "--title",
        path.stem,
        str(upload),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=NOTEBOOKLM_TIMEOUT_S,
        )
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


def _archive_direct_files(
    files: list[tuple[Path, str]], month: str, lines: list[str], stamp: str, dry: bool
) -> tuple[int, int]:
    archived = 0
    failed = 0
    for src, subdir in files:
        dest = _archive_dest(src, subdir, month)
        lines.append(f"{stamp} {'DRY ' if dry else ''}ARCHIVE {src} -> {dest}")
        if dry:
            archived += 1
            continue
        err = _move_file(src, dest)
        if err:
            failed += 1
            lines.append(f"{stamp} WARN archive failed {src}: {err}")
        else:
            archived += 1
    return archived, failed


def _ingest_file(
    src: Path, subdir: str, month: str, lines: list[str], stamp: str, dry: bool
) -> tuple[int, int, int]:
    dest = _archive_dest(src, subdir, month)
    lines.append(
        f"{stamp} {'DRY ' if dry else ''}INGEST {src} "
        f"-> NotebookLM title=\"{src.stem}\" -> {dest}"
    )
    if dry:
        return 1, 1, 0
    err = _ingest_source(src)
    if err:
        lines.append(f"{stamp} WARN ingest failed {src}: {err}")
        return 0, 0, 1
    err = _move_file(src, dest)
    if err:
        lines.append(f"{stamp} WARN archive failed after ingest {src}: {err}")
        return 1, 0, 1
    return 1, 1, 0


def _ingest_files(
    files: list[tuple[Path, str]], month: str, lines: list[str], stamp: str, dry: bool
) -> tuple[int, int, int, int]:
    if files and not dry and not NOTEBOOKLM.is_file():
        lines.append(
            f"{stamp} WARN NotebookLM binary missing at {NOTEBOOKLM}; "
            f"leaving {len(files)} ingestion candidate(s) in place"
        )
        return 0, 0, len(files), 0

    ingested = 0
    archived = 0
    failed = 0
    ingest_now = files[:INBOX_INGEST_CAP]
    skipped_by_cap = len(files) - len(ingest_now)
    for src, subdir in ingest_now:
        add_ingested, add_archived, add_failed = _ingest_file(
            src, subdir, month, lines, stamp, dry
        )
        ingested += add_ingested
        archived += add_archived
        failed += add_failed
    if skipped_by_cap:
        lines.append(
            f"{stamp} {'DRY ' if dry else ''}SKIP ingest cap reached: "
            f"{skipped_by_cap} expired NotebookLM candidate(s) left in place"
        )
    return ingested, archived, failed, skipped_by_cap


def expire_inbox(lines: list[str], stamp: str, dry: bool) -> list[str]:
    warnings: list[str] = []
    expired, scan_warnings, held = _expired_files()
    for warn in scan_warnings:
        warnings.append(warn)
        lines.append(f"{stamp} {warn}")
    for path, reason in held:
        lines.append(
            f"{stamp} HOLD {path} — looks client-critical/confidential ({reason}); "
            f"not archived. File it into a project tree."
        )

    month = time.strftime("%Y-%m")
    direct = [(p, subdir) for p, subdir, _mtime in expired if not _needs_ingest(p, subdir)]
    ingest = [(p, subdir) for p, subdir, _mtime in expired if _needs_ingest(p, subdir)]
    archived, failed = _archive_direct_files(direct, month, lines, stamp, dry)
    ingested, ingest_archived, ingest_failed, skipped_by_cap = _ingest_files(
        ingest, month, lines, stamp, dry
    )
    archived += ingest_archived
    failed += ingest_failed

    lines.append(
        f"{stamp} {'DRY ' if dry else ''}expiry summary: "
        f"ingested={ingested} archived={archived} failed={failed} "
        f"skipped-by-cap={skipped_by_cap} held={len(held)}"
    )
    return warnings


def _month_end_epoch(month: str) -> float | None:
    """Epoch seconds for the first instant AFTER the given YYYY-MM month."""
    try:
        year, mon = (int(part) for part in month.split("-"))
        nxt = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    except (ValueError, TypeError):
        return None
    return time.mktime(nxt.timetuple())


def reap_archive(lines: list[str], stamp: str, dry: bool) -> int:
    """Stage _archive/<YYYY-MM>/ folders past the TTL into deletions/.

    This is the sweep's terminal stage. Without it _archive is a one-way ratchet:
    files move Inbox -> _archive and stay forever (413 MB had accumulated by
    2026-08-01). Staging is NOT deletion — emptying deletions/ stays a human act.

    The month folder's NAME is the clock, deliberately not its mtime: archiving is a
    rename, which preserves each file's original mtime, so mtime records when the
    content was written, not when it was archived. Reaping by mtime would stage a
    just-archived file the moment it landed.
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
        # Record when this was STAGED. Nothing else here can: every mtime under dest
        # belongs to the original content, so a deletions reaper reading mtime would
        # see items as weeks old the day they arrive.
        try:
            (dest / STAGED_AT).write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} inbox_sweep reap\n")
        except OSError as exc:
            lines.append(f"{stamp} WARN could not write {STAGED_AT} in {dest}: {exc}")
        reaped += 1
    return reaped


def main() -> None:
    dry = "--dry-run" in sys.argv
    no_expire = "--no-expire" in sys.argv
    no_reap = "--no-reap" in sys.argv
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    _prepare_log(lines, stamp)
    files, warnings = candidates()
    for warn in warnings:
        lines.append(f"{stamp} {warn}")
    moves = 0
    for src in files:
        dest_dir = route(src)
        if dest_dir is None:
            continue
        dest = INBOX / dest_dir / src.name
        dest = _with_collision_suffix(dest)
        lines.append(f"{stamp} {'DRY ' if dry else ''}{src} -> {dest}")
        moves += 1
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
    if moves == 0 and not warnings:
        lines.append(f"{stamp} {'DRY ' if dry else ''}nothing to sweep")
    if no_expire:
        lines.append(f"{stamp} {'DRY ' if dry else ''}expiry skipped (--no-expire)")
    else:
        warnings += expire_inbox(lines, stamp, dry)
    if no_reap:
        lines.append(f"{stamp} {'DRY ' if dry else ''}archive reap skipped (--no-reap)")
    else:
        reaped = reap_archive(lines, stamp, dry)
        lines.append(
            f"{stamp} {'DRY ' if dry else ''}reap summary: "
            f"months-staged={reaped} ttl={ARCHIVE_TTL_DAYS}d"
        )
    _write_log(lines, stamp)
    print("\n".join(lines))
    if any("permission denied" in w for w in warnings):
        sys.exit(2)  # surface TCC denial to launchd so the scheduler watchdog alerts


if __name__ == "__main__":
    main()
