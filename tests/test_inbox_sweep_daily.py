"""Daily engine tests (inbox_sweep.run) against a temp home: Desktop/Downloads ingest,
bundle-as-unit, skip-recent, handoff 1-day rule, alias creation + 3-day expiry, cold
candidates, screenshot month, research ingest, lock. No network, no Finder, no launchd."""
import fcntl
import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("s", str(HERE / "inbox_sweep.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
rt = m.rt

fails = []
DAY = 86400


def check(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def touch(path, age_s=3 * DAY, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age_s
    os.utime(str(path), (t, t))
    return path


def age_tree(path, age_s):
    t = time.time() - age_s
    for d, ds, fs in os.walk(str(path)):
        for n in ds + fs:
            os.utime(os.path.join(d, n), (t, t))
    os.utime(str(path), (t, t))


class FakeRun:
    """Stand-in for subprocess.run for defaults/killall."""
    def __init__(self, current=""):
        self.calls = []
        self.current = current

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[1] == "read":
            rc = 0 if self.current else 1
            return subprocess.CompletedProcess(cmd, rc, stdout=self.current + "\n", stderr="")
        if cmd[1] == "write":
            self.current = cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


FLAGGED = set()   # paths the fake "Finder" marks as aliases (stands in for the FinderInfo flag)
rt.finder_alias_flag = lambda p: str(p) in FLAGGED


def fake_alias(target, folder, name):
    (Path(folder) / name).write_text("ALIAS->" + str(target))
    FLAGGED.add(str(Path(folder) / name))
    return None


with tempfile.TemporaryDirectory() as td:
    td = os.path.realpath(td)   # the router refuses symlinked ancestors (/var -> /private/var)
    home = Path(td) / "home"
    m.configure(home=home)
    m.STATE_DIR = Path(td) / "state"
    m.MANIFEST_DIR = Path(td) / "manifests"
    m.LOG = Path(td) / "sweep.log"
    rt.JEV_CACHE = Path(td) / "jev-cache.json"
    inbox = home / "Inbox"
    inbox.mkdir(parents=True)
    now = time.time()

    # ---- Desktop + Downloads + Inbox fixtures
    touch(home / "Desktop/sasha-notes.md", age_s=3600)
    touch(home / "Desktop/fresh-mishmash.md", age_s=30)                 # <10 min: leave
    touch(home / "Desktop/Projects", age_s=3600)                        # protected alias name
    touch(home / "Desktop/Inbox", age_s=3600)
    touch(home / "Desktop/movie.mov", age_s=3600)                        # not on allowlist
    (home / "Desktop/Some Folder").mkdir()
    touch(home / "Desktop/Some Folder/a.md", age_s=3600)                  # folders stay on Desktop
    touch(home / "Downloads/spindle-brief.pdf", age_s=5 * DAY)
    touch(home / "Downloads/recent-spindle.pdf", age_s=3600)             # <48h: leave
    touch(home / "Downloads/video.crdownload", age_s=5 * DAY)
    touch(home / "Downloads/spindle.html.part", age_s=5 * DAY)
    bundle = inbox / "notes/qa-run-sasha-2026-10-08"
    touch(bundle / "evidence/1.png")
    touch(bundle / "README.md", text="# Sasha QA\n")
    age_tree(bundle, 4 * DAY)
    touch(inbox / "notes/handoffs/2026-10-12-sasha-recent.md", age_s=DAY // 2)
    touch(inbox / "notes/handoffs/2026-09-20-sasha-old.md", age_s=20 * DAY)
    touch(inbox / "notes/2026-10-12-spindle-just-now.md", age_s=60)

    lines, warnings, summary = m.run(use_jev=False, do_ingest=False, do_screens=False, do_cold=False,
                                     now=now, alias_maker=fake_alias)
    # the Desktop belongs to desktop_clean.py since 2026-10-18: the sweep leaves it alone
    check("desktop file not touched by the sweep", (home / "Desktop/sasha-notes.md").read_text() == "x"
          and not any((inbox / "notes").glob("*/*/sasha-notes.md")))
    check("fresh desktop file left alone", (home / "Desktop/fresh-mishmash.md").read_text() == "x")
    check("Projects/Inbox aliases untouched", (home / "Desktop/Projects").read_text() == "x"
          and (home / "Desktop/Inbox").read_text() == "x")
    check("non-allowlisted file and folders stay", (home / "Desktop/movie.mov").exists()
          and (home / "Desktop/Some Folder/a.md").exists())
    check("downloads >48h routed", any((inbox / "pdfs/spindle").glob("*/spindle-brief.pdf")))
    check("downloads <48h and partials stay", (home / "Downloads/recent-spindle.pdf").exists()
          and (home / "Downloads/video.crdownload").exists() and (home / "Downloads/spindle.html.part").exists())
    check("bundle moved as one unit", any((inbox / "notes/sasha").glob("*/qa-run-sasha-2026-10-08/evidence/1.png")))
    check("recent inbox item skipped", (inbox / "notes/2026-10-12-spindle-just-now.md").exists())
    check("recent handoff stays flat", (inbox / "notes/handoffs/2026-10-12-sasha-recent.md").exists())
    check("old handoff filed", any((inbox / "notes/handoffs/sasha").glob("*/2026-09-20-sasha-old.md")))
    mf = list(m.MANIFEST_DIR.glob("*-sweep.jsonl"))
    check("plan manifest written before the moves", len(mf) == 1
          and any(r.get("op") == "move" and "status" not in r for r in rt.read_manifest(mf[0])))
    jr = rt.applied_path(mf[0])
    rows = rt.read_journal(jr)
    ops = [r.get("op") for r in rows]
    check("journal has an intent before each done row",
          ops.count("intent") == sum(1 for r in rows if r.get("status") == "done") and ops[-1] == "end"
          and all(ops.index("intent") < i for i, r in enumerate(rows) if r.get("status") == "done"))
    check("manifest has done rows and skip reasons", any(r.get("status") == "done" for r in rows)
          and any(r.get("reason") == "recent" for r in rt.read_manifest(mf[0])))
    report = list((inbox / "notes/mac-ops").glob("*/*-mac-ops-report-cleanup.md"))
    check("daily report written", len(report) == 1 and "items moved" in report[0].read_text())

    # ---- undo through the manifest restores everything
    res = rt.undo_rows([r for r in rows if r.get("status") == "done"], alias_state=m.STATE_DIR / "desktop-aliases.json",
                       inbox=inbox, allow_src_roots=[m.DESKTOP, m.DOWNLOADS])
    check("manifest undo restores originals", (home / "Downloads/spindle-brief.pdf").exists()
          and (inbox / "notes/handoffs/2026-09-20-sasha-old.md").exists()
          and all(r["undo"] == "restored" for r in res), [r["undo"] for r in res])

    # ---- legacy aliases (made before 2026-10-18) still expire through the tracked state
    target = touch(inbox / "notes/sasha/2026-10/sasha-notes.md")
    fake_alias(target, home / "Desktop", "sasha-alias.md")
    tracked = []
    m.record_alias(tracked, home / "Desktop/sasha-alias.md", str(target), now)
    m._save_json(m.STATE_DIR / "desktop-aliases.json", tracked)
    alias_path = home / "Desktop/sasha-alias.md"
    m.run(use_jev=False, do_ingest=False, do_screens=False, do_cold=False, now=now + 60, alias_maker=fake_alias)
    check("tracked alias is not ingested", alias_path.exists())

    # ---- alias expiry: kept at 2 days, removed after 3
    keep, removed = m.expire_desktop_aliases(now + 2 * DAY, [], "T", dry=False)
    check("alias kept before 3 days", removed == 0 and alias_path.exists())
    keep, removed = m.expire_desktop_aliases(now + 3 * DAY + 120, [], "T", dry=False)
    check("alias removed after 3 days", removed >= 1 and not alias_path.exists())
    check("moved item itself survives alias removal",
          any((inbox / "notes/sasha").glob("*/sasha-notes*")))
    check("state emptied", json.loads((m.STATE_DIR / "desktop-aliases.json").read_text()) == [])
    # a changed (replaced) alias path is never deleted
    other = touch(home / "Desktop/replaced.md", age_s=10)
    ino_wrong = os.stat(str(other)).st_ino + 12345
    entries = [{"alias": str(other), "target": "x", "created": now - 9 * DAY, "ino": ino_wrong}]
    m._save_json(m.STATE_DIR / "desktop-aliases.json", entries)
    lines2 = []
    m.expire_desktop_aliases(now, lines2, "T", dry=False)
    check("replaced file is not deleted by expiry", other.exists() and any("no longer matches" in l for l in lines2))
    big = home / "Desktop/bigfile.md"
    big.write_text("y" * 2_000_000)
    ino = os.stat(str(big)).st_ino
    m._save_json(m.STATE_DIR / "desktop-aliases.json",
                 [{"alias": str(big), "target": "x", "created": now - 9 * DAY, "ino": ino}])
    m.expire_desktop_aliases(now, [], "T", dry=False)
    check("large file is never treated as an alias", big.exists())

    # ---- alias removal needs inode + sha256 + Finder alias flag (BLOCK 5)
    tracked_now = []
    a_ok = home / "Desktop/alias-ok.md"
    fake_alias(Path("/x/target"), home / "Desktop", "alias-ok.md")
    m.record_alias(tracked_now, a_ok, "/x/target", now - 9 * DAY)
    e_ok = tracked_now[-1]
    check("alias entry records inode and sha256", e_ok["ino"] == os.stat(str(a_ok)).st_ino
          and e_ok["sha256"] == rt.sha256_file(str(a_ok)) and len(e_ok["sha256"]) == 64)
    check("untouched alias matches", rt.alias_matches(str(a_ok), e_ok))
    a_edit = home / "Desktop/alias-edit.md"
    fake_alias(Path("/x/t2"), home / "Desktop", "alias-edit.md")
    m.record_alias(tracked_now, a_edit, "/x/t2", now - 9 * DAY)
    a_edit.write_text("REAL USER CONTENT")                      # same inode, new content
    a_flag = home / "Desktop/alias-noflag.md"
    fake_alias(Path("/x/t3"), home / "Desktop", "alias-noflag.md")
    m.record_alias(tracked_now, a_flag, "/x/t3", now - 9 * DAY)
    FLAGGED.discard(str(a_flag))                                # no longer a Finder alias
    a_old = home / "Desktop/alias-legacy.md"
    fake_alias(Path("/x/t4"), home / "Desktop", "alias-legacy.md")
    m.record_alias(tracked_now, a_old, "/x/t4", now - 9 * DAY)
    tracked_now[-1].pop("sha256")                               # entry without a hash
    lines6 = []
    keep6, removed6 = m.expire_desktop_aliases(now, lines6, "T", dry=False, alias_state=list(tracked_now))
    check("only the verified alias is removed", removed6 == 1 and not a_ok.exists() and a_edit.exists()
          and a_flag.exists() and a_old.exists(), lines6)
    check("edited file kept and logged", a_edit.read_text() == "REAL USER CONTENT"
          and sum("no longer matches" in l for l in lines6) == 3, lines6)
    # undo removes a tracked alias only when it is still verifiably ours
    st_file = m.STATE_DIR / "desktop-aliases.json"
    m._save_json(st_file, tracked_now)
    mv_dst = inbox / "notes/sasha/2026-10/alias-edit.md"
    touch(mv_dst, text="MOVED")
    row_u = {"op": "move", "src": str(a_edit), "dst": str(mv_dst), "status": "done", "type": "notes",
             "name": "alias-edit.md"}
    und6 = rt.undo_rows([row_u], alias_state=st_file, inbox=inbox, allow_src_roots=[m.DESKTOP, m.DOWNLOADS])
    check("undo leaves an edited 'alias' alone", und6[0]["undo"] == "skipped-original-path-occupied"
          and a_edit.read_text() == "REAL USER CONTENT" and mv_dst.exists(), und6)

    # ---- startup reconcile: an unmatched intent from a crashed run is resolved
    moved_dst = touch(inbox / "notes/sasha/2026-10/zz-crash-src.md", text="C")
    jpath = m.MANIFEST_DIR / "20200101-000000-sweep.jsonl.applied.jsonl"
    j = rt.Journal(jpath)
    j.append({"op": "intent", "op2": 1, "src": str(inbox / "notes/zz-crash-src.md"), "dst": str(moved_dst),
              "type": "notes", "name": "zz-crash-src.md"})
    j.close()
    m.run(use_jev=False, do_ingest=False, do_screens=False, do_cold=False, now=now + 5, alias_maker=fake_alias)
    last = rt.read_journal(jpath)[-1]
    check("sweep reconciles interrupted moves at startup", last.get("status") == "done" and last.get("reconciled"), last)

    # ---- cold candidates (>30 days by newest mtime inside the bundle), pins, holds
    old_b = inbox / "notes/sasha/2026-08/old-bundle"
    touch(old_b / "x.md", age_s=60 * DAY)
    age_tree(old_b, 60 * DAY)
    mixed = inbox / "notes/sasha/2026-08/mixed-bundle"
    touch(mixed / "old.md", age_s=60 * DAY)
    touch(mixed / "sub/new.md", age_s=2 * DAY)                      # newest child keeps bundle warm
    touch(inbox / "notes/sasha/2026-08/pinned-note.md", age_s=60 * DAY)
    touch(inbox / "notes/sasha/2026-08/Broken English NDA.pdf", age_s=60 * DAY)
    touch(inbox / "notes/handoffs/spindle/2026-08/2026-08-01-spindle-only.md", age_s=60 * DAY)
    touch(inbox / "notes/sasha/2026-08/Posture AI Legal Review Packet/readme.md", age_s=60 * DAY)
    age_tree(inbox / "notes/sasha/2026-08/Posture AI Legal Review Packet", 60 * DAY)
    touch(inbox / ".pinned", text="pinned-note.md\n")
    cands, held = m.cold_candidates(now)
    cnames = {c["name"] for c in cands}
    hnames = {c["name"]: c["reason"] for c in held}
    check("old unpinned bundle is a candidate", "old-bundle" in cnames, cnames)
    check("bundle with a recent child is not", "mixed-bundle" not in cnames and "mixed-bundle" not in hnames)
    check("pinned item held", hnames.get("pinned-note.md") == "pinned")
    check("legal packet held by name", "Posture AI Legal Review Packet" in hnames, hnames)
    check("NDA held by name", "Broken English NDA.pdf" in hnames)
    check("latest handoff per project held", hnames.get("2026-08-01-spindle-only.md") == "latest-handoff-for-project")
    path = m.write_cold_candidates(cands, held, now, dry=False)
    check("candidate list written under mac-ops", "mac-ops-cleanup-candidates.md" in path.name
          and "old-bundle" in path.read_text() and old_b.exists())

    # ---- screenshots month
    fr = FakeRun()
    ch = m.advance_screenshot_location(now, [], "T", False, runner=fr)
    cur = _cur = time.strftime("%Y-%m", time.localtime(now))
    target = inbox / "screenshots/_unassigned" / cur
    check("screenshot location set when unset", ch and fr.current == str(target) and target.is_dir())
    check("killall SystemUIServer called", any(c[0].endswith("killall") for c in fr.calls))
    fr2 = FakeRun(current=str(inbox / "screenshots/_unassigned/2026-01"))
    ch = m.advance_screenshot_location(now, [], "T", False, runner=fr2)
    check("old month advanced, old folder kept", ch and fr2.current == str(target))
    fr3 = FakeRun(current="/Users/someone/Pictures")
    check("custom location left alone", not m.advance_screenshot_location(now, [], "T", False, runner=fr3)
          and fr3.current == "/Users/someone/Pictures")
    check("already current is a no-op", not m.advance_screenshot_location(now, [], "T", False, runner=FakeRun(str(target))))

    # ---- research ingest: only 'research' names created in the last day, once
    touch(inbox / "notes/sasha/2026-10/2026-10-13-sasha-research-pack.md", age_s=3600)
    touch(inbox / "notes/sasha/2026-10/2026-10-13-sasha-plain-note.md", age_s=3600)
    touch(inbox / "notes/sasha/2026-10/2026-09-01-sasha-research-old.md", age_s=3600)
    p_old = inbox / "notes/sasha/2026-10/2026-09-01-sasha-research-old.md"
    sent = []
    ok, failed = m.ingest_research(time.time(), [], "T", False, ingest=lambda p: sent.append(p.name))
    check("ingests research-named fresh notes only", "2026-10-13-sasha-research-pack.md" in sent
          and "2026-10-13-sasha-plain-note.md" not in sent and failed == 0, sent)
    n = len(sent)
    m.ingest_research(time.time(), [], "T", False, ingest=lambda p: sent.append(p.name))
    check("never ingested twice", len(sent) == n)

    # ---- lock: a held lock makes main() exit without work
    m.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(m.STATE_DIR / "sweep.lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    check("overlapping run is refused", m.main(["--dry-run"]) == 0)
    fh.close()

    # ---- reap_archive opt-in still works against the temp inbox
    arch = inbox / "_archive/2025-01/notes"
    arch.mkdir(parents=True)
    (arch / "o.md").write_text("old")
    check("reap_archive (opt-in) stages old months", m.reap_archive([], "T", dry=False) == 1)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
