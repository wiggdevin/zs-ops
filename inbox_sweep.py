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

Usage: inbox_sweep.py [--dry-run]
Log:   ~/Library/Logs/zs-ai-os/inbox-sweep.log
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

HOME = Path.home()
INBOX = HOME / "Inbox"
LOG = HOME / "Library/Logs/zs-ai-os/inbox-sweep.log"
DOWNLOADS_MIN_AGE_S = 48 * 3600

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SCREENSHOT_RE = re.compile(r"(screenshot|screen shot|qa-|_preview)", re.IGNORECASE)
ROUTES = {".html": "html", ".md": "notes", ".pdf": "pdfs",
          ".json": "misc", ".csv": "misc", ".txt": "misc"}


def route(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in ROUTES:
        return ROUTES[ext]
    if ext in IMAGE_EXTS:
        return "screenshots" if SCREENSHOT_RE.search(path.name) else "misc"
    return None  # not on allowlist — never moved


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


def main() -> None:
    dry = "--dry-run" in sys.argv
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    files, warnings = candidates()
    for warn in warnings:
        lines.append(f"{stamp} {warn}")
    moves = 0
    for src in files:
        dest_dir = route(src)
        if dest_dir is None:
            continue
        dest = INBOX / dest_dir / src.name
        if dest.exists():
            suffix = time.strftime("-%Y%m%d-%H%M%S")
            dest = dest.with_name(dest.stem + suffix + dest.suffix)
        lines.append(f"{stamp} {'DRY ' if dry else ''}{src} -> {dest}")
        moves += 1
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
    if moves == 0 and not warnings:
        lines.append(f"{stamp} {'DRY ' if dry else ''}nothing to sweep")
    with LOG.open("a") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if any("permission denied" in w for w in warnings):
        sys.exit(2)  # surface TCC denial to launchd so the scheduler watchdog alerts


if __name__ == "__main__":
    main()
