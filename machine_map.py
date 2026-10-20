#!/usr/bin/env python3
"""
machine_map.py — regenerate ~/MACHINE.md, the living INVENTORY of this machine.

Read-only janitor (launchd: com.zs.machine-map, Mondays 09:00; or run by hand):
  python3 ~/.config/zs-ops/machine_map.py

Sections: Projects (activity + size), Storage (ranked sizes incl. caches),
Memory (zs-memory store count). Only ever writes ~/MACHINE.md. Stdlib only.

Scope note (2026-07-03 map consolidation): topology and health — launchd jobs,
ports, MCP-client wiring — live in zs-os-doctor (~/projects/tools/zs-os-doctor,
map/declared.json is the source of truth). This script is deliberately just the
disk/activity inventory; its old Services section moved into the doctor as
declared-edge invariants. zs-system-map was archived, superseded by the doctor.
"""
from __future__ import annotations  # launchd runs /usr/bin/python3 (3.9): no PEP 604 at runtime

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from glob import glob

HOME = os.path.expanduser("~")
OUT = os.path.join(HOME, "MACHINE.md")
PROJECTS = os.path.join(HOME, "projects")
CONTAINERS = {"archive", "clients", "services", "tools", "plugins"}
ACTIVE_DAYS, STALE_DAYS = 14, 90


def run(cmd, timeout=120):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def du_kb(path):
    out = run(["du", "-sk", path], timeout=600)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return 0


def human(kb):
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.0f} MB"
    return f"{kb} KB" if kb else "—"


def last_activity(path):
    """ISO date of last git commit, else newest shallow mtime."""
    date = run(["git", "-C", path, "log", "-1", "--format=%cs"])
    if date:
        return date, "git"
    try:
        ts = max((e.stat().st_mtime for e in os.scandir(path)), default=0)
        return (datetime.fromtimestamp(ts).date().isoformat(), "mtime") if ts else ("—", "—")
    except OSError:
        return "—", "—"


def status_of(date_str, archived):
    if archived:
        return "archived"
    try:
        age = (datetime.now().date() - datetime.fromisoformat(date_str).date()).days
    except ValueError:
        return "unknown"
    return "active" if age < ACTIVE_DAYS else "stale" if age < STALE_DAYS else "dormant"


def node_modules_kb(path):
    out = run(["find", path, "-maxdepth", "3", "-name", "node_modules",
               "-prune", "-print"], timeout=300)
    return sum(du_kb(p) for p in out.splitlines() if p)


def project_rows():
    rows = []
    for entry in sorted(os.scandir(PROJECTS), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        if entry.name in CONTAINERS:
            for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
                if sub.is_dir():
                    rows.append((f"{entry.name}/{sub.name}", sub.path, entry.name == "archive"))
        else:
            rows.append((entry.name, entry.path, False))
    table = []
    for name, path, archived in rows:
        date, src = last_activity(path)
        table.append({
            "name": name, "date": date, "src": src,
            "status": status_of(date, archived),
            "kb": du_kb(path), "nm_kb": node_modules_kb(path),
        })
    return table


def storage_section():
    lines = ["## Storage (ranked)", ""]
    tops = []
    for entry in os.scandir(HOME):
        if entry.is_dir() and not entry.name.startswith("."):
            tops.append((du_kb(entry.path), f"~/{entry.name}"))
    lines.append("### Top-level home dirs")
    for kb, name in sorted(tops, reverse=True):
        lines.append(f"- {human(kb):>9}  {name}")
    lines.append("")
    caches = {
        "~/Library/Caches": f"{HOME}/Library/Caches",
        "~/Library/Developer (Xcode)": f"{HOME}/Library/Developer",
        "~/.ollama (models)": f"{HOME}/.ollama",
        # 2026-08-01: the "~/.colima (docker VM)" entry was dropped here when the
        # runtime moved to OrbStack. It is deliberately NOT replaced by an
        # OrbStack entry: this section is for HIDDEN dirs the top-level scan
        # skips, and OrbStack keeps its disk image in ~/OrbStack (visible, so
        # already ranked above) rather than ~/.orbstack (a 300K run dir).
        "~/.npm": f"{HOME}/.npm",
        "pnpm store": f"{HOME}/Library/pnpm",
        "uv cache": f"{HOME}/Library/Caches/uv",
        "~/.cache": f"{HOME}/.cache",
        "~/.cargo": f"{HOME}/.cargo",
        "Homebrew cache": f"{HOME}/Library/Caches/Homebrew",
    }
    lines.append("### Caches, models, VMs (hidden dirs)")
    sized = [(du_kb(p), label) for label, p in caches.items() if os.path.exists(p)]
    for kb, label in sorted(sized, reverse=True):
        lines.append(f"- {human(kb):>9}  {label}")
    return lines


def latest_dream_digests(root: str | None = None) -> list[str]:
    """Dream digests oldest to newest, by the date in the file name. Covers the nested Inbox
    layout (notes/<project>/<YYYY-MM>/<date>-<project>-dream-digest.md) and legacy flat
    notes/dream-digest-<date>.md; `_archive` copies are ignored."""
    root = root or f"{HOME}/Inbox/notes"
    found = glob(f"{root}/**/*dream-digest*.md", recursive=True)

    def key(path: str):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        return (m.group(1) if m else "", os.path.getmtime(path))

    return sorted((p for p in found if "/_archive/" not in p and re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(p))), key=key)


def memory_section():
    lines = ["## Memory (zs-memory)", ""]
    # Scoped `automation` token — this section only counts memories, so it has
    # no business holding the primary token's any-namespace/any-delete authority.
    token = os.environ.get("ZS_MEMORY_TOKEN") or run(
        [f"{HOME}/bin/zsvault", "get", "zs_memory_token_automation"])
    total = "?"
    if token:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8765/api/v1/memories/?user_id=zero-fleet&size=1&page=1",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                total = json.loads(resp.read()).get("total", "?")
        except Exception:
            total = "unreachable"
    lines.append(f"- store: {total} memories (user_id=zero-fleet, 127.0.0.1:8765)")
    digests = latest_dream_digests()
    lines.append(f"- latest dream digest: {digests[-1] if digests else 'none yet'}")
    db_dir = f"{HOME}/.local/share/zs-memory"
    if os.path.exists(db_dir):
        lines.append(f"- db size (incl. WAL): {human(du_kb(db_dir))}")
    return lines


def main():
    projects = project_rows()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# MACHINE.md — living machine inventory",
        f"regenerated {now} by ~/.config/zs-ops/machine_map.py "
        "(weekly: com.zs.machine-map, Mon 09:00)",
        "",
        "## Projects",
        "",
        "| project | last activity | status | size | node_modules |",
        "|---|---|---|---|---|",
    ]
    order = {"active": 0, "stale": 1, "dormant": 2, "archived": 3, "unknown": 4}
    for p in sorted(projects, key=lambda p: (order.get(p["status"], 9), -p["kb"])):
        lines.append(f"| {p['name']} | {p['date']} ({p['src']}) | {p['status']} "
                     f"| {human(p['kb'])} | {human(p['nm_kb']) if p['nm_kb'] else '—'} |")
    lines.append("")
    lines += storage_section() + [""] + memory_section() + [
        "",
        "_Services, ports, and MCP wiring: see the zs-os-doctor weekly report_",
        "_(~/Inbox/html/mac-ops/<YYYY-MM>/*os-doctor-report*.html; find it with ~/bin/inbox-find --type html os-doctor-report; source of truth: zs-os-doctor/map/declared.json)._",
    ]
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(projects)} projects)")


if __name__ == "__main__":
    main()
