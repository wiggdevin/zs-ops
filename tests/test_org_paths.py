"""inbox_paths tests: alias resolution, allocate contract, find over new + legacy layouts."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "org"))
import inbox_paths as ip  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ---- resolve_project
check("be does not match bench", ip.resolve_project("bench-results") == "_unassigned")
check("be as whole text", ip.resolve_project("be") == "broken-english")
check("be as first token", ip.resolve_project("be-nda") == "broken-english")
check("be in the middle does not match", ip.resolve_project("to-be-done") == "_unassigned")
check("mm alias only leading", ip.resolve_project("mm-sprint") == "mishmash" and
      ip.resolve_project("summary-mm-x") == "_unassigned")
check("longest alias wins", ip.resolve_project("mishh-mashh-2-tidy-2026-09-30.md") == "mishmash")
check("gate-box ticket alias", ip.resolve_project("gate-box") == "mishmash")
check("strong beats weak generic word", ip.resolve_project("2026-10-01-sasha-gemini-design-audit.md") == "sasha")
check("weak alias still routes alone", ip.resolve_project("design-toolbox-audit") == "design-research")
check("spaces and case", ip.resolve_project("High Frequency INFO") == "high-frequency")
check("unknown", ip.resolve_project("random vacation photos") == "_unassigned")
check("empty", ip.resolve_project("") == "_unassigned")

# ---- allocate
with tempfile.TemporaryDirectory() as td:
    ip.INBOX = Path(td) / "Inbox"
    d = dt.date(2026, 10, 13)
    p = ip.allocate("mm", "plans", "Source Kits!", ext="md", date=d)
    check("path shape", str(p) == str(ip.INBOX / "plans/mishmash/2026-10/2026-10-13-mishmash-source-kits.md"), p)
    check("parent created, name reserved by an empty file", p.parent.is_dir() and p.is_file()
          and p.stat().st_size == 0)
    p.write_text("x")
    p2 = ip.allocate("mishmash", "plans", "source kits", ext=".md", date=d)
    check("collision -02", p2.name.endswith("source-kits-02.md"), p2.name)
    p2.write_text("x")
    p3 = ip.allocate("mishmash", "plans", "source kits", ext="md", date=d)
    check("collision -03", p3.name.endswith("source-kits-03.md"), p3.name)
    r1 = ip.allocate("sasha", "notes", "reserve me", ext="md", date=d)
    r2 = ip.allocate("sasha", "notes", "reserve me", ext="md", date=d)   # r1 never written
    check("unwritten allocation still reserves its name", r1 != r2 and r2.name.endswith("reserve-me-02.md"), (r1, r2))
    # a file that appears at the candidate name between our look and our create is skipped
    import threading
    taken = ip.INBOX / "notes/sasha/2026-10/2026-10-13-sasha-racy.md"
    taken.write_text("theirs")
    r3 = ip.allocate("sasha", "notes", "racy", ext="md", date=d)
    check("exclusive create never reuses an existing file", r3.name.endswith("racy-02.md")
          and taken.read_text() == "theirs", r3)
    results = []
    def grab():
        results.append(ip.allocate("sasha", "notes", "parallel", ext="md", date=d))
    ts = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("parallel allocations get distinct names", len({str(x) for x in results}) == 8, results)
    h = ip.allocate("sasha", "handoffs", "init prompt", ext="md", date=d)
    check("handoffs maps to notes/handoffs", "/notes/handoffs/sasha/2026-10/" in str(h), h)
    u = ip.allocate("who knows", "misc", "thing", ext="txt", date=d)
    check("unknown project -> _unassigned, name has no slug", "/misc/_unassigned/2026-10/2026-10-13-thing.txt" in str(u), u)
    b = ip.allocate("sasha", "notes", "Onboarding review", bundle=True, date=d)
    check("bundle dir created, no ext", b.is_dir() and b.suffix == "", b)
    b2 = ip.allocate("sasha", "notes", "Onboarding review", bundle=True, date=d)
    check("bundle collision -02", b2.name.endswith("-02") and b2.is_dir(), b2)
    s = ip.allocate("sasha", "html", "sasha-report", ext="html", date=d)
    check("no doubled project slug", s.name == "2026-10-13-sasha-report.html", s.name)
    long = ip.allocate("sasha", "notes", "x" * 200, ext="md", date=d)
    check("subject <= 64 chars", len(long.stem) <= len("2026-10-13-sasha-") + 64, long.name)
    check("slugify strips traversal", ip.slugify("../../etc/passwd") == "etc-passwd")
    check("slugify unicode", ip.slugify("Café ÅÄÖ plan") == "cafe-aao-plan")
    try:
        ip.allocate("sasha", "bogus", "x")
        check("bad type rejected", False)
    except ValueError:
        check("bad type rejected", True)
    try:
        ip.allocate("sasha", "notes", "x", ext="../x")
        check("bad ext rejected", False)
    except ValueError:
        check("bad ext rejected", True)

    # ---- find over new layout + legacy flat
    root = ip.INBOX
    legacy = root / "notes" / "2026-10-01-sasha-old-audit.md"
    legacy.write_text("old")
    os.utime(legacy, (1_700_000_000, 1_700_000_000))
    newer = ip.allocate("sasha", "notes", "fresh audit", ext="md", date=d)
    newer.write_text("new")
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    flat_ho = root / "notes" / "handoffs" / "2026-10-12-sasha-live.md"
    flat_ho.parent.mkdir(parents=True, exist_ok=True)
    flat_ho.write_text("live")
    os.utime(flat_ho, (1_790_000_000, 1_790_000_000))
    other = root / "plans" / "2026-10-02-spindle-plan.md"
    other.write_text("p")
    got = ip.find(project="sasha", limit=20)
    mt = [os.stat(str(g)).st_mtime for g in got]
    check("find is sorted newest first", mt == sorted(mt, reverse=True))
    check("find spans new layout, flat handoff and legacy flat", newer in got and flat_ho in got and legacy in got)
    check("fresh before live before legacy", got.index(newer) < got.index(flat_ho) < got.index(legacy))
    check("find excludes other projects", other not in got)
    found_h = ip.find(type="handoffs", project="sasha")
    check("find type=handoffs", flat_ho in found_h and h in found_h and len(found_h) == 2, found_h)
    check("find query", ip.find(query="old audit") == [legacy])
    check("find latest limit", len(ip.find(project="sasha", limit=1)) == 1)
    for d_ in (root / "notes", root / "notes/sasha", root / "notes/sasha/2026-10"):
        (d_ / "Icon\r").write_text("")
    check("find ignores Finder Icon\\r at every level", all(not x.name.startswith("Icon") for x in ip.find(limit=0)))
    check("find ignores admin dirs", all("Potential Deletes" not in str(x) for x in ip.find(limit=100)))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
