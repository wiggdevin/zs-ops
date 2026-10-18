"""desktop_clean.run tests against a temp home: aliases and symlinks trashed, duplicates
trashed (Inbox copy and Desktop twin), installed .dmg trashed, shared-token items bundled,
folders routed and renamed, git repos and recent items left, manifest undo. No Finder, no
hdiutil, no network."""
import importlib.util
import os
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dc", str(HERE / "desktop_clean.py"))
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


FLAGGED = set()
rt.finder_alias_flag = lambda p: str(p) in FLAGGED
TRASHED = []


def fake_trash(path):
    TRASHED.append(str(path))
    m.TRASH.mkdir(exist_ok=True)
    os.rename(str(path), str(m.TRASH / path.name))
    return None


with tempfile.TemporaryDirectory() as td:
    td = os.path.realpath(td)
    home = Path(td) / "home"
    m.configure(home=home)
    m.STATE_DIR = Path(td) / "state"
    m.MANIFEST_DIR = Path(td) / "manifests"
    rt.JEV_CACHE = Path(td) / "jev-cache.json"
    desk, inbox = home / "Desktop", home / "Inbox"
    inbox.mkdir(parents=True)
    (home / "Applications/HighLevel.app").mkdir(parents=True)

    touch(desk / "Projects")
    touch(desk / "Inbox")
    touch(desk / "old-alias.md", text="ALIAS")
    FLAGGED.add(str(desk / "old-alias.md"))
    os.symlink(str(inbox), str(desk / "link-to-inbox"))
    touch(inbox / "screenshots/_unassigned/2026-10/shot.png", text="SAME-BYTES")
    touch(desk / "Screenshot 2026-10-14 at 6.29.17 PM.png", text="SAME-BYTES")  # Inbox copy
    touch(desk / "notes-a.md", text="TWIN")
    touch(desk / "notes-b.md", text="TWIN")                                          # Desktop twin
    touch(desk / "HighLevel-0.1.12-universal.dmg", text="dmg")
    touch(desk / "Other-2.0.dmg", text="other dmg")                                  # app not installed
    for n in ("Mug35_3D/a.glb", "Mug35_sides/b.png"):
        touch(desk / n)
    touch(desk / "mug35_hf_hero.mp4", text="hero")
    age_tree(desk / "Mug35_3D", 3 * DAY)
    age_tree(desk / "Mug35_sides", 3 * DAY)
    touch(desk / "Sasha Meal Companion - project setup/README.md", text="# Sasha setup\n")
    age_tree(desk / "Sasha Meal Companion - project setup", 3 * DAY)
    touch(desk / "Screenshot 2026-10-18 at 10.51.17 AM.png", text="unique shot")
    touch(desk / "thing.webloc", text="url")
    touch(desk / "repo/.git/HEAD")
    age_tree(desk / "repo", 3 * DAY)
    touch(desk / "fresh.md", age_s=30)

    apps_of = lambda dmg: ["HighLevel.app"] if "HighLevel" in dmg.name else ["Other.app"]
    lines, summary, rc = m.run(use_jev=False, apps_of=apps_of, trasher=fake_trash)
    left = sorted(os.listdir(str(desk)))
    check("exit code 0", rc == 0, "\n".join(lines))
    check("only Projects, Inbox, the repo and the fresh file stay",
          left == ["Inbox", "Projects", "fresh.md", "repo"], left)
    check("alias and symlink trashed", str(desk / "old-alias.md") in TRASHED and str(desk / "link-to-inbox") in TRASHED)
    check("file with an Inbox copy trashed", any("6.29.17" in t for t in TRASHED))
    check("one Desktop twin trashed, one kept",
          sum(t.endswith(("notes-a.md", "notes-b.md")) for t in TRASHED) == 1
          and len(list(inbox.glob("notes/_unassigned/*/*notes-*.md"))) == 1)
    check("installed dmg trashed", str(desk / "HighLevel-0.1.12-universal.dmg") in TRASHED)
    check("uninstalled dmg routed to misc", len(list(inbox.glob("misc/_unassigned/*/*-other-2-0.dmg"))) == 1,
          list(inbox.rglob("*.dmg")))
    bundles = list(inbox.glob("misc/_unassigned/*/*-mug35"))
    check("shared-token items consolidated into one bundle", len(bundles) == 1
          and sorted(os.listdir(str(bundles[0]))) == ["Mug35_3D", "Mug35_sides", "mug35_hf_hero.mp4"],
          [list(b.iterdir()) for b in bundles])
    check("folder routed by project and renamed",
          len(list(inbox.glob("notes/sasha/*/*-sasha-meal-companion-project-setup/README.md"))) == 1,
          list(inbox.glob("*/sasha/*/*")))
    check("screenshot renamed by its date",
          (inbox / "screenshots/_unassigned/2026-10/2026-10-18-screenshot-10-51-17-am.png").exists(),
          list(inbox.glob("screenshots/_unassigned/*/*")))
    check("unknown type routed to misc", len(list(inbox.glob("misc/_unassigned/*/*-thing.webloc"))) == 1)
    check("git repo logged as WARN", any("WARN git repo" in l for l in lines))
    mf = list(m.MANIFEST_DIR.glob("*-desktop.jsonl"))
    check("manifest written", len(mf) == 1 and any(r.get("op") == "trash" for r in rt.read_manifest(mf[0])))

    # second run: a new mug35 item joins the same bundle
    touch(desk / "mug35_hf_loop.mp4", text="loop")
    m.run(use_jev=False, apps_of=apps_of, trasher=fake_trash, now=time.time() + 1)
    check("later item joins the existing bundle (no new bundle)",
          len(list(inbox.glob("misc/_unassigned/*/*-mug35*"))) == 1
          and (bundles[0] / "mug35_hf_loop.mp4").exists(), list(inbox.glob("misc/_unassigned/*/*")))
    # .. only when it is the third: a single new item with no group is just routed
    # undo restores the first run's moves
    rows = [r for r in rt.read_journal(rt.applied_path(mf[0])) if r.get("status") == "done"]
    res = rt.undo_rows(rows, inbox=inbox, allow_src_roots=[desk])
    check("undo restores moved items", (desk / "Sasha Meal Companion - project setup/README.md").exists()
          and (desk / "Mug35_3D/a.glb").exists() and all(r["undo"] == "restored" for r in res),
          [r["undo"] for r in res])

    check("names: already-dated names are kept",
          m.clean_name("2026-10-01-sasha-x.md", "sasha", None, False) == "2026-10-01-sasha-x.md")
    check("lead token skips generic words", m.lead_token("Screenshot 2026") is None
          and m.lead_token("Mug35_3D") == "mug35")

print("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails))
raise SystemExit(1 if fails else 0)
