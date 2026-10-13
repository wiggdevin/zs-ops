#!/usr/bin/env python3
"""org_daily_review — daily stale-item pass for ~/Inbox (run by Hermes cron at 08:00).

1. Finds filed items (~/Inbox/<type>/<project>/<YYYY-MM>/<item>) whose newest file is more
   than STALE_DAYS old and that are not pinned (a `.pin` file in the item or a name ending in
   `.pin`), not inside a git repo, not symlinked and not reviewed as "keep" in the last
   COOLDOWN_DAYS. Also lists quarantined items in `Potential Deletes`.
2. Asks GPT-6.1 Sol (codex exec, read-only sandbox, subscription OAuth) to decide per item:
   keep | archive | potential_delete. Only names, sizes and ages are sent — never contents.
   Replies are validated; anything malformed falls back to the default.
3. Applies decisions safely: archive → verified tar.zst on DevinBackUp (org_archive.py, which
   re-checks symlinks, git and source changes itself); potential_delete → moved to
   ~/Inbox/Potential Deletes/<YYYY-MM>/ under the archive lock, never over an existing path.
   Nothing is ever deleted. If Codex fails, stale items are archived (reversible) and
   Potential Deletes are only reported. One item's failure never stops the batch.
4. Prints a short summary (Hermes delivers stdout) and appends it to today's mac-ops report.

Flags: --dry-run (decide, write nothing), --no-codex, --limit N (default 25 items).
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import org_archive  # noqa: E402

HOME = Path.home()
INBOX = HOME / "Inbox"
TYPES = ["html", "plans", "pdfs", "screenshots", "notes", "misc", "notes/handoffs"]
PD = INBOX / "Potential Deletes"
STALE_DAYS = 30
COOLDOWN_DAYS = 30
DECISIONS = ("keep", "archive", "potential_delete")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
ORG_DIR = Path(__file__).resolve().parent
MANIFEST = ORG_DIR / "manifests" / "daily-review.jsonl"
COOLDOWN = ORG_DIR / "state" / "review-cooldown.json"
CODEX = "/opt/homebrew/bin/codex"
# Same spirit as inbox_sweep's protected-name guard: these stay local and are never auto-moved.
PROTECTED_RE = re.compile(r"\blegal\b|privileged|restricted|\bhealth\b|contract|\bnda\b|invoice|credential|secret|passport|\btax", re.I)
PD_SKIP = {"README.md"}


def skip_name(name):
    return name.startswith(".") or name == "Icon\r"


def real_dirs(parent):
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return []
    return [e for e in entries if not skip_name(e.name) and not e.is_symlink() and e.is_dir()]


def newest_mtime(p):
    newest = os.lstat(p).st_mtime
    if p.is_dir() and not p.is_symlink():
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for n in files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
                except OSError:
                    pass
    return newest


def size_and_count(p):
    if not p.is_dir() or p.is_symlink():
        return os.lstat(p).st_size, 1
    s = c = 0
    for root, _, files in os.walk(p):
        for n in files:
            try:
                s += os.lstat(os.path.join(root, n)).st_size
                c += 1
            except OSError:
                pass
    return s, c


def pinned(p):
    return p.name.endswith(".pin") or (p.is_dir() and (p / ".pin").exists())


def load_cooldown():
    try:
        return json.loads(COOLDOWN.read_text())
    except (OSError, ValueError):
        return {}


def candidates(limit, cooldown):
    now = time.time()
    out = []
    for t in TYPES:
        troot = INBOX / t
        if troot.is_symlink() or not troot.is_dir():
            continue
        for proj in real_dirs(troot):
            if proj.name == "handoffs":
                continue
            for month in real_dirs(proj):
                if not MONTH_RE.match(month.name):
                    continue
                try:
                    items = sorted(month.iterdir())
                except OSError:
                    continue
                for item in items:
                    try:
                        if (skip_name(item.name) or item.name.endswith(".archived.md") or item.is_symlink() or pinned(item)
                                or PROTECTED_RE.search(item.name)):
                            continue
                        if now - cooldown.get(str(item), 0) < COOLDOWN_DAYS * 86400:
                            continue
                        age = (now - newest_mtime(item)) / 86400
                        if age < STALE_DAYS:
                            continue
                        if org_archive.check_source(item):
                            continue  # symlinked path, git repo or outside Inbox: never a candidate
                        size, count = size_and_count(item)
                    except OSError:
                        continue
                    out.append({"path": str(item), "type": t, "project": proj.name, "name": item.name,
                                "age_days": int(age), "bytes": size, "files": count})
    out.sort(key=lambda x: -x["age_days"])
    out = out[:limit]
    for i, it in enumerate(out):
        it["id"] = i
    pd_items = []
    pd_entries = []
    if PD.is_dir() and not PD.is_symlink():
        for e in sorted(PD.iterdir()):
            if skip_name(e.name) or e.name in PD_SKIP:
                continue
            if MONTH_RE.match(e.name) and e.is_dir() and not e.is_symlink():
                pd_entries += [c for c in sorted(e.iterdir()) if not skip_name(c.name)]
            else:
                pd_entries.append(e)
    for item in pd_entries:
        # rename updates ctime, so ctime approximates the time the item entered quarantine
        age = (now - os.lstat(item).st_ctime) / 86400
        pd_items.append({"path": str(item), "name": item.name, "quarantine_days": int(age)})
    return out, pd_items


def ask_codex(items):
    packet = [{k: i[k] for k in ("id", "type", "project", "name", "age_days", "bytes", "files")} for i in items]
    prompt = (
        "You review stale items in Devin's ~/Inbox (agent outputs: notes, plans, html reports, screenshots, misc bundles). "
        "Each item below is older than 30 days. Decide per item: 'keep' (still likely useful locally: canonical plans, "
        "credentials/contracts/legal/invoices, recent-project handoffs worth local access), "
        "'archive' (cold but possibly useful later: move to the external backup drive; this is the default), or "
        "'potential_delete' (clearly throwaway: temp/probe/test runs, duplicates like '(1)' copies, empty or tiny scratch). "
        "Nothing is deleted automatically; potential_delete only quarantines. "
        "Reply with ONLY a JSON array of objects {\"id\": int, \"decision\": str, \"reason\": str (<=12 words)}.\n\n"
        + json.dumps(packet))
    try:
        r = subprocess.run([CODEX, "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                            "-s", "read-only", "--skip-git-repo-check", "-C", str(HOME), prompt],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"codex error: {e}"
    if r.returncode != 0:
        return None, f"codex exit {r.returncode}"
    m = re.findall(r"\[\s*\{.*?\}\s*\]", r.stdout, re.S)
    if not m:
        return None, "codex gave no JSON"
    try:
        raw = json.loads(m[-1])
    except ValueError:
        return None, "codex JSON parse error"
    valid_ids = {i["id"] for i in items}
    decisions = {}
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict):
            continue
        i, dec, reason = d.get("id"), d.get("decision"), d.get("reason", "")
        if type(i) is not int or i not in valid_ids or i in decisions or dec not in DECISIONS:
            continue
        decisions[i] = {"decision": dec, "reason": reason if isinstance(reason, str) else ""}
    return decisions, ""


def log(entry):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    fd = os.open(MANIFEST, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(entry) + "\n").encode())
        org_archive.full_sync(fd)
    finally:
        os.close(fd)


def quarantine(item):
    src = Path(item["path"])
    err = org_archive.check_source(src)
    if err:
        raise RuntimeError(err)
    dest_dir = PD / datetime.now().strftime("%Y-%m")
    with org_archive.archive_lock():
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 2
        while os.path.lexists(dest):
            dest = dest_dir / f"{src.stem}-{n:02d}{src.suffix}"
            n += 1
        entry = {"op": "quarantine", "src": str(src), "dest": str(dest),
                 "undo": shlex.join(["mv", "-n", str(dest), str(src)])}
        log({**entry, "state": "prepare"})
        os.rename(src, dest)
        log({**entry, "state": "done"})
    return str(dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    cooldown = load_cooldown()
    items, pd_items = candidates(args.limit, cooldown)
    decisions, note = ({}, "codex skipped") if args.no_codex or not items else ask_codex(items)
    if decisions is None:
        decisions = {}
    counts = {"keep": 0, "archive": 0, "potential_delete": 0, "failed": 0}
    lines = []
    try:
        for it in items:
            d = decisions.get(it["id"], {"decision": "archive", "reason": "default (no review)"})
            dec, res = d["decision"], ""
            if not args.dry_run:
                try:
                    if dec == "archive":
                        r = org_archive.archive(it["path"], it["project"])
                        if not r.get("ok"):
                            dec, res = "failed", r.get("error", "")
                    elif dec == "potential_delete":
                        res = quarantine(it)
                    if dec in ("keep", "failed"):
                        cooldown[it["path"]] = time.time()
                except Exception as e:  # one item never stops the batch
                    dec, res = "failed", f"{type(e).__name__}: {e}"
                    cooldown[it["path"]] = time.time()
            counts[dec] += 1
            lines.append(f"- {dec}: {it['type']}/{it['project']}/{it['name']} ({it['age_days']}d, "
                         f"{it['bytes'] // 1024} KB) — {d.get('reason', '')} {res}".rstrip())
    finally:
        old_pd = [p for p in pd_items if p["quarantine_days"] >= 30]
        today = datetime.now()
        summary = (f"Inbox daily review {today:%Y-%m-%d}{' (dry run)' if args.dry_run else ''}: "
                   f"{len(items)} stale items — archived {counts['archive']}, quarantined {counts['potential_delete']}, "
                   f"kept {counts['keep']}, failed {counts['failed']}. Potential Deletes: {len(pd_items)} items, "
                   f"{len(old_pd)} waiting 30+ days for Devin." + (f" Note: {note}" if note else ""))
        out = "\n".join([summary] + lines + [f"- review: {p['name']} ({p['quarantine_days']}d in Potential Deletes)"
                                             for p in old_pd])
        if not args.dry_run:
            COOLDOWN.parent.mkdir(parents=True, exist_ok=True)
            tmp = COOLDOWN.with_suffix(".tmp")
            tmp.write_text(json.dumps(cooldown))
            os.replace(tmp, COOLDOWN)
            report_dir = INBOX / "notes" / "mac-ops" / today.strftime("%Y-%m")
            report_dir.mkdir(parents=True, exist_ok=True)
            with open(report_dir / f"{today:%Y-%m-%d}-mac-ops-report-cleanup.md", "a") as f:
                f.write("\n## Daily stale review (Hermes / GPT-6.1 Sol)\n\n" + out + "\n")
        print(out)


if __name__ == "__main__":
    main()
