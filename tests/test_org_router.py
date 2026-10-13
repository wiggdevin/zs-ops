"""org_router tests against a temp Inbox: bundles move whole, recency, handoff 1-day rule,
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
    td = os.path.realpath(td)   # macOS temp dirs sit behind a /var symlink; the router refuses those
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
    touch(root / "notes/handoffs/2026-10-12-sasha-recent.md", age_s=DAY // 2)
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
    check("git bundle is never planned (is or contains a repo)", "Terroir-2-Redesign" not in mv
          and sk.get("Terroir-2-Redesign") == "git-repo", sk)
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
    check("git bundle stays where it was", (root / "misc/Terroir-2-Redesign/source/.git/HEAD").exists())
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


# ======================================================================= review fixes
class Boom(BaseException):
    """Simulated crash (not caught by `except Exception`)."""


def fresh_inbox(td_name, files=()):
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix=td_name)))
    r = base / "Inbox"
    r.mkdir()
    ip.INBOX = r
    for rel in files:
        touch(r / rel)
    return base, r


def row_for(src, dst, **kw):
    d = {"op": "move", "src": str(src), "dst": str(dst), "type": "notes", "name": Path(src).name,
         "kind": "file", "project": "sasha", "has_git": False}
    d.update(kw)
    return d


import shutil

# BLOCK 1: atomic no-replace rename, both directions, race between check and rename
base, r = fresh_inbox("race-")
try:
    a_, b_ = base / "a.txt", base / "b.txt"
    a_.write_text("A")
    b_.write_text("B")
    try:
        rt.rename_noreplace(a_, b_)
        check("rename_noreplace refuses an existing file", False)
    except FileExistsError:
        check("rename_noreplace refuses an existing file", b_.read_text() == "B" and a_.read_text() == "A")
    d1, d2 = base / "d1", base / "d2"
    (d1 / "x").mkdir(parents=True)
    (d2 / "y").mkdir(parents=True)
    try:
        rt.rename_noreplace(d1, d2)
        check("rename_noreplace refuses an existing directory", False)
    except FileExistsError:
        check("rename_noreplace refuses an existing directory", (d2 / "y").is_dir() and (d1 / "x").is_dir())
    rt.rename_noreplace(a_, base / "c.txt")
    check("rename_noreplace moves when free", (base / "c.txt").read_text() == "A" and not a_.exists())

    src = touch(r / "notes/race-notes.md", text="SRC")
    dst = r / "notes/sasha/2026-10/race-notes.md"
    rt._TEST_HOOK = lambda stage, s_, d_: (touch(Path(d_), text="NEWCOMER", age_s=0)
                                          if stage == "before-rename" else None)
    try:
        out = rt.apply_rows([row_for(src, dst)], inbox=r)
    finally:
        rt._TEST_HOOK = None
    check("apply never overwrites a file created after the check",
          out[0]["status"] == "skipped-dst-exists" and dst.read_text() == "NEWCOMER" and src.read_text() == "SRC", out)
    dst.unlink()
    done = rt.apply_rows([row_for(src, dst)], inbox=r)
    check("apply then moves normally", done[0]["status"] == "done" and dst.read_text() == "SRC")
    rt._TEST_HOOK = lambda stage, s_, d_: (touch(Path(d_), text="OCCUPIER", age_s=0)
                                          if stage == "before-undo-rename" else None)
    try:
        und = rt.undo_rows(done, inbox=r, allow_src_roots=[])
    finally:
        rt._TEST_HOOK = None
    check("undo never overwrites a file created after the check",
          und[0]["undo"] == "skipped-original-path-occupied" and src.read_text() == "OCCUPIER"
          and dst.read_text() == "SRC", und)
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# BLOCK 2: intent row (fsynced) before the rename, result row after, reconcile after a crash
base, r = fresh_inbox("journal-")
try:
    src = touch(r / "notes/j-notes.md", text="J")
    dst = r / "notes/sasha/2026-10/j-notes.md"
    jp = base / "j.applied.jsonl"
    j = rt.Journal(jp)
    seen = []
    rt._TEST_HOOK = lambda stage, s_, d_: seen.append([x.get("op") for x in rt.read_journal(jp)])
    try:
        rt.apply_rows([row_for(src, dst)], inbox=r, journal=j)
    finally:
        rt._TEST_HOOK = None
    j.close()
    ops = [x.get("op") + ":" + str(x.get("status", "")) for x in rt.read_journal(jp)]
    check("intent is on disk before the rename", seen == [["intent"]], seen)
    check("journal order is intent then done", ops == ["intent:", "move:done"], ops)
    # crash after the intent, before the rename: nothing moved, reconcile says so
    src2 = touch(r / "notes/k-notes.md", text="K")
    dst2 = r / "notes/sasha/2026-10/k-notes.md"
    jp2 = base / "k.applied.jsonl"
    j2 = rt.Journal(jp2)

    def crash(stage, s_, d_):
        raise Boom()
    rt._TEST_HOOK = crash
    try:
        rt.apply_rows([row_for(src2, dst2)], inbox=r, journal=j2)
    except Boom:
        pass
    finally:
        rt._TEST_HOOK = None
    j2.close()
    check("crash leaves an unmatched intent", [x["op"] for x in rt.read_journal(jp2)] == ["intent"])
    n = rt.reconcile_all(base)
    ops2 = [(x["op"], x.get("status")) for x in rt.read_journal(jp2)]
    check("reconcile: not moved is recorded", n >= 1 and ops2[-1] == ("move", "skipped-interrupted-not-moved")
          and src2.exists() and not dst2.exists(), ops2)
    check("reconcile is idempotent", rt.reconcile_all(base) == 0)
    # crash after the rename, before the receipt: reconcile records done, undo can reverse it
    src3 = touch(r / "notes/m-notes.md", text="M")
    dst3 = r / "notes/sasha/2026-10/m-notes.md"
    jp3 = base / "m.applied.jsonl"
    j3 = rt.Journal(jp3)
    j3.append(dict(row_for(src3, dst3), op="intent"))
    j3.close()
    rt.mkdirs_under(dst3.parent, r)
    os.rename(str(src3), str(dst3))
    check("reconcile: moved without receipt becomes done", rt.reconcile_journal(jp3) == 1
          and rt.read_journal(jp3)[-1]["status"] == "done")
    und = rt.undo_rows(rt.read_journal(jp3), inbox=r, allow_src_roots=[])
    check("undo works from a reconciled journal", und[0]["undo"] == "restored" and src3.read_text() == "M")
    # torn last line is ignored
    with open(str(jp3), "a") as fh:
        fh.write('{"op": "intent", "src": "/x')
    check("torn journal line ignored", len(rt.read_journal(jp3)) == 2)
    jt = rt.Journal(jp3)
    jt.append({"op": "end"})
    jt.close()
    check("append after a torn line starts a fresh line", rt.read_journal(jp3)[-1] == {"op": "end"}
          and len(rt.read_journal(jp3)) == 3)
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# BLOCK 3: git repos are never moved: is / contains / sits inside
base, r = fresh_inbox("git-")
try:
    touch(r / "misc/clone/.git/HEAD")
    touch(r / "misc/clone/src.txt")
    age_tree(r / "misc/clone", 3 * DAY)
    touch(r / "misc/plain-note.txt")
    rows_g = rt.build_plan(use_jev=False)
    skg = {x["name"]: x["reason"] for x in rows_g if x["op"] == "skip"}
    check("plan skips a bundle containing .git", skg.get("clone") == "git-repo", skg)
    forced = rt.apply_rows([row_for(r / "misc/clone", r / "misc/sasha/2026-10/clone", type="misc")], inbox=r)
    check("apply re-checks git and refuses", forced[0]["status"] == "skipped-git-repo"
          and (r / "misc/clone/.git/HEAD").exists(), forced)
    # a file that sits inside a repo (worktree file, nested path)
    inner = touch(r / "misc/clone/src.txt")
    forced2 = rt.apply_rows([row_for(inner, r / "misc/sasha/2026-10/src.txt", type="misc")], inbox=r)
    check("apply refuses an item inside a repo", forced2[0]["status"] == "skipped-git-repo", forced2)
finally:
    shutil.rmtree(str(base), ignore_errors=True)
base = Path(os.path.realpath(tempfile.mkdtemp(prefix="gitanc-")))
try:
    (base / ".git").mkdir()
    r = base / "Inbox"
    r.mkdir()
    ip.INBOX = r
    touch(r / "notes/plain-sasha.md")
    rows_a = rt.build_plan(use_jev=False)
    check("Inbox sitting inside a git repo: everything skipped",
          not [x for x in rows_a if x["op"] == "move"]
          and any(x.get("reason") == "git-repo" for x in rows_a), rows_a)
    forced = rt.apply_rows([row_for(r / "notes/plain-sasha.md", r / "notes/sasha/2026-10/plain-sasha.md")], inbox=r)
    check("apply inside a repo refused", forced[0]["status"] == "skipped-git-repo", forced)
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# BLOCK 4: symlinked ancestors cannot escape the Inbox
base, r = fresh_inbox("sym-")
try:
    outside = base / "outside"
    outside.mkdir()
    os.symlink(str(outside), str(r / "notes"))
    touch(r / "plans/p-sasha.md")
    rows_s = rt.build_plan(use_jev=False)
    check("a symlinked type dir is skipped by plan", any(x.get("reason") == "unsafe-path" for x in rows_s)
          and not any(x["op"] == "move" and x["type"] == "notes" for x in rows_s), rows_s)
    os.unlink(str(r / "notes"))
    (r / "notes").mkdir()
    touch(r / "notes/escape-sasha.md")
    os.symlink(str(outside), str(r / "notes/sasha"))
    res_s = rt.apply_rows([row_for(r / "notes/escape-sasha.md", r / "notes/sasha/2026-10/escape-sasha.md")], inbox=r)
    check("apply refuses a dst reached through a symlink",
          res_s[0]["status"] == "skipped-unsafe-path" and not list(outside.rglob("*")), res_s)
    # src through a symlinked ancestor
    real_dir = base / "realdir"
    touch(real_dir / "x-sasha.md")
    age_tree(real_dir, 3 * DAY)
    os.symlink(str(real_dir), str(r / "misc"))
    res_s2 = rt.apply_rows([row_for(r / "misc/x-sasha.md", r / "plans/sasha/2026-10/x-sasha.md")], inbox=r)
    check("apply refuses a src reached through a symlink", res_s2[0]["status"] == "skipped-unsafe-path"
          and (real_dir / "x-sasha.md").exists(), res_s2)
    # undo: original path under a symlinked ancestor
    fake_done = row_for(r / "misc/y-sasha.md", r / "plans/sasha/2026-10/y-sasha.md", status="done")
    touch(r / "plans/sasha/2026-10/y-sasha.md", text="Y")
    und_s = rt.undo_rows([fake_done], inbox=r, allow_src_roots=[])
    check("undo refuses an original path behind a symlink", und_s[0]["undo"] == "skipped-unsafe-path"
          and not (real_dir / "y-sasha.md").exists(), und_s)
    # mkdirs_under refuses a symlink component
    try:
        rt.mkdirs_under(r / "notes/sasha/2026-11", r)
        check("mkdirs_under refuses a symlink component", False)
    except OSError:
        check("mkdirs_under refuses a symlink component", not (outside / "2026-11").exists())
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# BLOCK 6: receipts are append-only and apply resumes
base, r = fresh_inbox("resume-")
try:
    touch(r / "notes/one-sasha.md")
    touch(r / "notes/two-sasha.md")
    rows_r = rt.build_plan(use_jev=False)
    man = base / "man.jsonl"
    rt.write_manifest(rows_r, man)
    out = rt.main(["apply", "--manifest", str(man), "--max", "1"])
    jr = rt.applied_path(man)
    first = jr.read_text()
    done1 = [x for x in rt.read_journal(jr) if x.get("status") == "done"]
    check("first apply moved one (cap)", out == 0 and len(done1) == 1, done1)
    rt.main(["apply", "--manifest", str(man)])
    second = jr.read_text()
    done2 = [x for x in rt.read_journal(jr) if x.get("status") == "done"]
    check("re-apply appends: earlier receipts are byte-identical", second.startswith(first) and len(second) > len(first))
    check("re-apply resumes: the second item moves, the first is not repeated",
          len(done2) == 2 and len({(x["src"], x["dst"]) for x in done2}) == 2, done2)
    check("re-apply reports already-done", any(x.get("status") == "already-done" for x in rt.read_journal(jr)))
    rt.main(["apply", "--manifest", str(man)])
    check("third apply changes no moves", len([x for x in rt.read_journal(jr) if x.get("status") == "done"]) == 2)
    rt.main(["undo", "--manifest", str(man)])
    check("undo from the appended journal restores both", (r / "notes/one-sasha.md").exists()
          and (r / "notes/two-sasha.md").exists())
    check("undo receipt written", (Path(str(man) + ".undone.jsonl")).exists())
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# WARN 7: an incomplete scan blocks the move
base, r = fresh_inbox("scan-")
try:
    b = r / "notes/locked-bundle-sasha"
    touch(b / "ok.md")
    touch(b / "locked/inner.md")
    age_tree(b, 3 * DAY)
    os.chmod(str(b / "locked"), 0)
    try:
        nw, gg, inc = rt.walk_info(b)
        check("walk_info flags an unreadable directory", inc is True)
        rows_l = rt.build_plan(use_jev=False)
        skl = {x["name"]: x["reason"] for x in rows_l if x["op"] == "skip"}
        check("plan skips a bundle whose scan failed", skl.get("locked-bundle-sasha") == "scan-incomplete", skl)
        forced = rt.apply_rows([row_for(b, r / "notes/sasha/2026-10/locked-bundle-sasha", kind="dir")], inbox=r)
        check("apply refuses an incomplete scan", forced[0]["status"] == "skipped-scan-incomplete"
              and b.exists(), forced)
    finally:
        os.chmod(str(b / "locked"), 0o755)
    big = r / "notes/big-bundle"
    for i in range(6):
        touch(big / ("f%d.txt" % i))
    check("walk_info flags the entry cap", rt.walk_info(big, cap=3)[2] is True
          and rt.walk_info(big, cap=100)[2] is False)
finally:
    shutil.rmtree(str(base), ignore_errors=True)

# WARN 9: apply re-checks the 7-day handoff rule
base, r = fresh_inbox("handoff-")
try:
    h = touch(r / "notes/handoffs/2026-10-12-sasha-live.md", age_s=DAY // 2)
    res_h = rt.apply_rows([row_for(h, r / "notes/handoffs/sasha/2026-10/2026-10-12-sasha-live.md", type="handoffs")],
                          inbox=r)
    check("apply refuses a handoff younger than 1 day", res_h[0]["status"] == "skipped-handoff-7d" and h.exists(), res_h)
    old = touch(r / "notes/handoffs/2026-09-01-sasha-old.md", age_s=40 * DAY)
    res_h2 = rt.apply_rows([row_for(old, r / "notes/handoffs/sasha/2026-09/2026-09-01-sasha-old.md", type="handoffs")],
                           inbox=r)
    check("apply files an old handoff", res_h2[0]["status"] == "done")
finally:
    shutil.rmtree(str(base), ignore_errors=True)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
