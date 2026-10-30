"""Reaper tests for inbox_sweep.reap_archive — run against a temp Inbox."""
import importlib.util, os, tempfile, time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "s", str(Path(__file__).resolve().parents[1] / "inbox_sweep.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []

def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        fails.append(name)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    m.INBOX = root / "Inbox"
    m.DELETIONS = m.INBOX / "deletions"
    arch = m.INBOX / "_archive"

    # Old month -> should reap. Recent month (the current one) -> should stay.
    month = time.strftime("%Y-%m")
    (arch / "2026-01" / "notes").mkdir(parents=True)
    (arch / "2026-01" / "notes" / "old.md").write_text("stale\n")
    (arch / month / "notes").mkdir(parents=True)
    recent = arch / month / "notes" / "fresh.md"
    recent.write_text("recent\n")
    # THE TRAP: archived this month, but content mtime is 2 years old (mv preserves it).
    ancient = time.time() - (730 * 86400)
    os.utime(recent, (ancient, ancient))
    (arch / "not-a-month").mkdir()

    lines = []
    reaped = m.reap_archive(lines, "TEST", dry=False)

    check("old month reaped", reaped == 1, f"reaped={reaped}")
    check("old month gone from _archive", not (arch / "2026-01").exists())
    check("recent month untouched", (arch / month / "notes" / "fresh.md").exists())
    check("ancient mtime did NOT trigger reap",
          (arch / month).exists(),
          "mtime leaked into the TTL decision")
    check("malformed month folder skipped, not crashed", (arch / "not-a-month").exists())
    check("malformed logged", any("unrecognized month" in l for l in lines))

    staged = list(m.DELETIONS.glob("*-archive-expiry/2026-01"))
    check("staged into deletions", len(staged) == 1, f"found={staged}")
    if staged:
        check("content preserved", (staged[0] / "notes" / "old.md").read_text() == "stale\n")
        marker = staged[0] / m.STAGED_AT
        check(".staged-at written", marker.is_file())
        if marker.is_file():
            check(".staged-at holds today's date",
                  time.strftime("%Y-%m-%d") in marker.read_text())

    # Idempotence: a second run must not re-reap or explode.
    lines2 = []
    check("second run is a no-op", m.reap_archive(lines2, "TEST", dry=False) == 0)

    # Dry run must not move anything.
    (arch / "2025-03").mkdir(parents=True)
    lines3 = []
    m.reap_archive(lines3, "TEST", dry=True)
    check("dry-run moves nothing", (arch / "2025-03").exists())
    check("dry-run still logs the intent", any("REAP" in l for l in lines3))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
raise SystemExit(1 if fails else 0)
