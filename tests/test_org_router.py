"""org_router tests against a temp Inbox: bundles move whole, recency, handoff 7-day rule,
skip list, symlinks, .git flag, already-filed, no-overwrite, undo, Jev threshold + cache."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "org"))
import inbox_paths as ip  # noqa: E402
import org_router as rt  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def touch(path, age_s=3600 * 24 * 3, text="x"):
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


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "Inbox"
    ip.INBOX = root
    rt.JEV_CACHE = Path(td) / "jev-cache.json"
    DAY = 86400
    touch(root / "notes/2026-10-05-sasha-audit.md")
    # bundle with nested stuff: children must not be routed on their own
    b = root / "notes/qa-bundle-2026-10-09"
    touch(b / "a/b/c.png")
    touch(b / "README.md", text="# Mishmash QA bundle\n")
    age_tree(b, 3 * 86400)
    # git-bearing bundle
    g = root / "misc/Terroir-2-Redesign"
    touch(g / "source/.git/HEAD")
    touch(g / "source/index.html")
    age_tree(g, 3 * 86400)
    # recently modified item and a bundle with one fresh child
    touch(root / "notes/just-written-mishmash.md", age_s=30)
    live = root / "notes/live-bundle-sasha"
    touch(live / "old.txt")
    age_tree(live, 3 * 86400)
    touch(live / "deep/new.txt", age_s=20)
    # handoffs: recent stays flat, old gets filed
    touch(root / "notes/handoffs/2026-10-12-sasha-recent.md", age_s=2 * DAY)
    touch(root / "notes/handoffs/2026-09-01-sasha-old.md", age_s=40 * DAY)
    # skip list
    touch(root / "misc/mish-team-packs-v2/x.txt")
    age_tree(root / "misc/mish-team-packs-v2", 3 * 86400)
    touch(root / "plans/2026-10-13-machine-org-design-sol.md")
    # symlink + dotfile
    (root / "misc").mkdir(exist_ok=True)
    os.symlink(str(root / "notes"), str(root / "misc/link-to-notes"))
    touch(root / "misc/.DS_Store")
    # already filed and admin
    touch(root / "plans/mishmash/2026-10/2026-10-07-mishh-filed.md")
    touch(root / "Potential Deletes/readme.txt")
    touch(root / "notes/handoffs/mishmash/2026-09/h.md")
    # unresolved by alias
    touch(root / "notes/mystery-thing.txt")
    # undated name: month from mtime
    ts = time.mktime((2026, 8, 20, 12, 0, 0, 0, 0, -1))
    p = root / "notes/undated-mishmash-notes.md"
    touch(p)
    os.utime(str(p), (ts, ts))

    touch(root / "notes/Icon\r", age_s=3 * DAY)
    touch(root / "misc/Icon\r", age_s=3 * DAY)
    rows = rt.build_plan(use_jev=False)
    mv = {r["name"]: r for r in rows if r["op"] == "move"}
    sk = {r["name"]: r["reason"] for r in rows if r["op"] == "skip"}

    check("file routed by alias + name date", mv["2026-10-05-sasha-audit.md"]["dst"] ==
          str(root / "notes/sasha/2026-10/2026-10-05-sasha-audit.md"), mv.get("2026-10-05-sasha-audit.md"))
    check("bundle is one unit (one row, children not listed)",
          "qa-bundle-2026-10-09" in mv and not any("/qa-bundle-2026-10-09/" in r["src"] for r in rows if r["op"] != "meta"))
    check("bundle project from README title", mv["qa-bundle-2026-10-09"]["project"] == "mishmash"
          and mv["qa-bundle-2026-10-09"]["basis"] == "title", mv["qa-bundle-2026-10-09"])
    check("git bundle routed whole and flagged", mv["Terroir-2-Redesign"]["has_git"] is True
          and mv["Terroir-2-Redesign"]["project"] == "terroir")
    check("recent file skipped", sk.get("just-written-mishmash.md") == "recent", sk)
    check("bundle with a fresh child skipped as recent", sk.get("live-bundle-sasha") == "recent", sk)
    check("recent handoff stays flat", sk.get("2026-10-12-sasha-recent.md") == "handoff-7d", sk)
    check("old handoff filed under handoffs/<project>/<month>",
          mv["2026-09-01-sasha-old.md"]["dst"] == str(root / "notes/handoffs/sasha/2026-09/2026-09-01-sasha-old.md"),
          mv.get("2026-09-01-sasha-old.md"))
    check("skip-list item skipped", sk.get("mish-team-packs-v2") == "skip-list")
    check("deferred run doc skipped", sk.get("2026-10-13-machine-org-design-sol.md") == "skip-list")
    check("symlink skipped", sk.get("link-to-notes") == "symlink")
    check("Finder Icon\\r ignored", not any(r.get("name") == "Icon\r" for r in rows))
    check("dotfile ignored", ".DS_Store" not in mv and ".DS_Store" not in sk)
    check("already filed / admin dirs untouched",
          not any(n in mv or n in sk for n in ("mishmash", "Potential Deletes")))
    check("handoffs dir itself not routed", "handoffs" not in mv)
    check("unresolved -> _unassigned without jev", mv["mystery-thing.txt"]["project"] == "_unassigned")
    check("undated month from mtime", mv["undated-mishmash-notes.md"]["month"] == "2026-08"
          and mv["undated-mishmash-notes.md"]["month_basis"] == "mtime")
    check("name month parsers", rt.name_month("a-20261009-b") == "2026-10" and rt.name_month("x-2026-13-45") is None
          and rt.name_month("v12026100912") is None)

    # apply + no-overwrite + undo
    manifest = Path(td) / "m.jsonl"
    rt.write_manifest(rows, manifest)
    res = rt.apply_rows(rt.read_manifest(manifest))
    st = {r["name"]: r["status"] for r in res}
    check("apply moved file", st["2026-10-05-sasha-audit.md"] == "done"
          and (root / "notes/sasha/2026-10/2026-10-05-sasha-audit.md").exists()
          and not (root / "notes/2026-10-05-sasha-audit.md").exists())
    check("bundle moved with all children", (root / "notes/mishmash/2026-10/qa-bundle-2026-10-09/a/b/c.png").exists())
    check("git dir untouched inside moved bundle",
          (root / "misc/terroir/2026-10/Terroir-2-Redesign/source/.git/HEAD").exists()
          or any((root / "misc/terroir").glob("*/Terroir-2-Redesign/source/.git/HEAD")))
    check("skipped items still in place", (root / "misc/mish-team-packs-v2/x.txt").exists()
          and (root / "notes/just-written-mishmash.md").exists())
    # second plan: filed items are left alone, nothing to do for them
    rows2 = rt.build_plan(use_jev=False)
    names2 = {r["name"] for r in rows2 if r["op"] == "move"}
    check("replan does not re-route filed items", "2026-10-05-sasha-audit.md" not in names2
          and "qa-bundle-2026-10-09" not in names2, names2)
    # no overwrite: occupy a destination, then apply a stale row
    touch(root / "notes/clash-notes.txt")
    victim = [r for r in rt.build_plan(use_jev=False) if r["op"] == "move" and r["name"] == "clash-notes.txt"][0]
    dst = Path(victim["dst"])
    touch(dst, text="OCCUPIED")
    out = rt.apply_rows([dict(victim)])
    check("never overwrites an existing destination", out[0]["status"] == "skipped-dst-exists"
          and dst.read_text() == "OCCUPIED" and Path(victim["src"]).exists(), out)
    dst.unlink()
    # apply guards: dst outside inbox, src outside allowed roots
    bad = dict(victim, dst=str(Path(td) / "elsewhere/x.txt"))
    check("dst outside inbox refused", rt.apply_rows([bad])[0]["status"] == "failed-dst-outside-inbox")
    bad2 = dict(victim, src=str(Path(td) / "outside.txt"))
    check("src outside roots refused", rt.apply_rows([bad2])[0]["status"] == "failed-src-outside-allowed-roots")
    # cap
    cap = rt.apply_rows([dict(victim)], max_moves=0)
    check("move cap honoured", cap[0]["status"] == "skipped-cap")
    # undo
    und = rt.undo_rows(res)
    ustat = {r["name"]: r["undo"] for r in und}
    check("undo restored file", ustat["2026-10-05-sasha-audit.md"] == "restored"
          and (root / "notes/2026-10-05-sasha-audit.md").exists())
    check("undo restored bundle", (root / "notes/qa-bundle-2026-10-09/a/b/c.png").exists())
    check("undo prunes the empty dirs it created", not (root / "notes/sasha/2026-10").exists())
    occupied = [r for r in res if r["name"] == "2026-10-05-sasha-audit.md"]
    again = rt.undo_rows(occupied)
    check("undo is idempotent", again and again[0]["undo"] == "skipped-item-missing")

    # Jev: threshold, closed set, cache, secret-name guard, privacy of snippet
    sent = []

    def fake_eval(state, questions, timeout=0):
        sent.append(json.dumps(state))
        ans = {}
        for i, k in enumerate(sorted(questions)):
            item = state["items"][k[1:]]
            if "pitch" in item["name"]:
                ans[k] = {"choice": "sasha", "p": 0.95}
            elif "maybe" in item["name"]:
                ans[k] = {"choice": "spindle", "p": 0.60}
            else:
                ans[k] = {"choice": "_unassigned", "p": 0.99}
        return {"answers": ans}

    touch(root / "notes/pitch-ideas.md", text="# Pitch ideas\nsecret=abc12345def\nmore text")
    touch(root / "notes/maybe-thing.txt")
    touch(root / "notes/api-key-notes.md", text="# keys\nsk-abcdefghijklmnopqrstuvwxyz")
    touch(root / "notes/service-agreement-draft.md", text="# Agreement\nconfidential terms here")
    rows3 = rt.build_plan(use_jev=True, evaluate=fake_eval)
    m3 = {r["name"]: r for r in rows3 if r["op"] == "move"}
    check("jev p>=0.85 accepted", m3["pitch-ideas.md"]["project"] == "sasha" and m3["pitch-ideas.md"]["basis"] == "jev")
    check("jev p<0.85 -> _unassigned", m3["maybe-thing.txt"]["project"] == "_unassigned"
          and m3["maybe-thing.txt"]["basis"] == "jev-unsure")
    payload = " ".join(sent)
    check("secret-looking name never sent", "api-key-notes" not in payload)
    check("contract-looking name sent without contents", "service-agreement-draft" in payload
          and "confidential terms" not in payload)
    check("excerpt capped at 300 chars", all(len(v["excerpt"]) <= 300 for s in sent
                                             for v in json.loads(s)["items"].values()))
    n_calls = len(sent)
    rows4 = rt.build_plan(use_jev=True, evaluate=fake_eval)
    m4 = {r["name"]: r for r in rows4 if r["op"] == "move"}
    check("rerun is served from the cache (no new jev call for cached items)",
          m4["pitch-ideas.md"]["basis"] == "jev-cache" and m4["pitch-ideas.md"]["project"] == "sasha"
          and len(sent) == n_calls, (len(sent), n_calls))

    def failing(state, questions, timeout=0):
        raise RuntimeError("down")
    touch(root / "notes/zzz-new-unknown.txt")
    try:
        rows5 = rt.build_plan(use_jev=True, evaluate=failing)
        ok = True
    except Exception as exc:  # jev outage must not crash planning
        ok, rows5 = False, []
    # the injected evaluate raises RuntimeError, which jev_pick treats as a generic Exception
    check("jev outage -> _unassigned, planning continues", ok and
          {r["name"]: r for r in rows5 if r["op"] == "move"}["zzz-new-unknown.txt"]["project"] == "_unassigned")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
