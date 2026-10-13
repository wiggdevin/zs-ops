#!/usr/bin/python3
"""finder_views.py - make every controlled Inbox folder open as a list sorted newest first.

Enumerates ~/Inbox, each type dir, notes/handoffs, each registered project dir and each
YYYY-MM dir, then runs finder_views.applescript (Finder must be running; it opens and closes
a window per folder). `--verify` re-opens each folder in a fresh window and reads the view and
sort column back. Only touches Finder view state (.DS_Store), never files.

    finder_views.py [--apply] [--verify] [--list]
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inbox_paths as ip  # noqa: E402

SCRIPT = HERE / "finder_views.applescript"
CHUNK = 25


def folders():
    out = [ip.INBOX]
    slug_set = set(ip.slugs())
    tdirs = [ip.INBOX / t for t in ip.inbox_types()]
    tdirs.append(ip.INBOX / "notes" / "handoffs")
    for t in tdirs:
        if not t.is_dir():
            continue
        out.append(t)
        for p in sorted(t.iterdir()):
            if p.name in slug_set and p.is_dir() and not p.is_symlink():
                out.append(p)
                for m in sorted(p.iterdir()):
                    if ip.MONTH_RE.match(m.name) and m.is_dir() and not m.is_symlink():
                        out.append(m)
    return out


def run_apply(paths):
    rows = []
    for i in range(0, len(paths), CHUNK):
        res = subprocess.run(["/usr/bin/osascript", str(SCRIPT)] + [str(p) for p in paths[i:i + CHUNK]],
                             capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            print("osascript failed:", res.stderr.strip(), file=sys.stderr)
            continue
        rows += [l.split("\t") for l in res.stdout.splitlines() if l.strip()]
    return rows


VERIFY = '''on run argv
	set report to ""
	tell application "Finder"
		repeat with p in argv
			set w to make new Finder window to folder ((POSIX file (p as text)) as alias)
			set o to list view options of w
			set sc to sort column of o
			set report to report & (p as text) & tab & (current view of w as text) & " / " & (name of sc as text) & " / " & (sort direction of sc as text) & linefeed
			close w
		end repeat
	end tell
	return report
end run
'''


def run_verify(paths):
    rows = []
    for i in range(0, len(paths), CHUNK):
        res = subprocess.run(["/usr/bin/osascript", "-e", VERIFY] + [str(p) for p in paths[i:i + CHUNK]],
                             capture_output=True, text=True, timeout=300)
        rows += [l.split("\t") for l in res.stdout.splitlines() if l.strip()]
    return rows


def main(argv):
    paths = folders()
    if "--list" in argv:
        print("\n".join(str(p) for p in paths))
        return 0
    if "--apply" in argv or not argv:
        rows = run_apply(paths)
        bad = [r for r in rows if len(r) < 2 or r[1].startswith("ERROR")]
        print("applied %d folders, %d errors" % (len(rows), len(bad)))
        for r in bad:
            print("  ", r)
    if "--verify" in argv:
        rows = run_verify(paths)
        ok = [r for r in rows if len(r) == 2 and r[1] == "list view / modification date column / reversed"]
        print("verified %d/%d folders: list view, Date Modified" % (len(ok), len(paths)))
        for r in rows:
            if r not in ok:
                print("  ", r)
        return 0 if len(ok) == len(paths) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
