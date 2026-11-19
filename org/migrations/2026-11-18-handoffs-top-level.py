#!/usr/bin/python3
"""One-off: move ~/Inbox/notes/handoffs -> ~/Inbox/handoffs and rename every handoff to
<YYYY-MM-DD>-<CODE>-<subject>.<ext> under <project>/<YYYY-MM>/. Writes a JSONL manifest
(old -> new). Leaves notes/handoffs as a symlink to ../handoffs. --apply to act."""
import datetime as dt, json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inbox_paths as ip
import org_router as rt

SRC = ip.INBOX / "notes" / "handoffs"
DST = ip.handoffs_dir()
APPLY = "--apply" in sys.argv
if SRC.is_symlink():  # already migrated: SRC is the compatibility link to DST
    sys.exit("already migrated: %s is a symlink; nothing to do" % SRC)
DATE_RES = [(re.compile(r"(20\d\d)-(\d\d)-(\d\d)"), "%Y-%m-%d"), (re.compile(r"(20\d\d)(\d\d)(\d\d)"), "%Y%m%d")]

def find_date(name, mtime):
    for rx, _ in DATE_RES:
        m = rx.search(name)
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
            except ValueError:
                pass
    return dt.date.fromtimestamp(mtime), None

def subject_of(stem, datestr, slug):
    s = stem.replace(datestr, " ") if datestr else stem
    s = re.sub(r"-([01]\d|2[0-3])[0-5]\d$", "", ip._norm(s))  # trailing HHMM
    words = {ip._norm(a) for p in ip.load_registry()["projects"] if p["slug"] == slug
             for a in (p.get("aliases") or []) + [p["slug"], p.get("code") or ""]}
    words.discard("")
    for w in sorted(words, key=len, reverse=True):  # drop a leading project word
        if s == w:
            s = ""
        elif s.startswith(w + "-"):
            s = s[len(w) + 1:]
    return ip.slugify(s) if s else "session"

# explicit routing for handoffs whose name and title carry no project alias (read 2026-11-18)
OVERRIDES = {
    "shutdown-index": "mac-ops", "mini-worker-followups": "mac-ops", "memguard": "mac-ops",
    "codegraph-mini": "mac-ops", "ci-cost-and-agent-env": "mac-ops", "github-enterprise": "mac-ops",
    "2026-10-29-session-wrap": "mac-ops", "wrap-2026-10-15": "mac-ops", "agent-browser-rollout": "mac-ops",
    "cswap": "mac-ops", "atlas-builds": "design-research", "2026-10-14-wrap.md": "clones",
    "nous-and-alet": "clones", "rea-study-wrap": "clones", "photoroom": "clones",
    "discovery-future-plans": "zs-business", "business-os": "zs-business", "dashboard-light-theme": "zs-business",
    "2026-10-17-session-close": "zs-business", "t3-audit-and-integration": "zs-business", "zs-one-stop": "zs-business",
    "rc1-wrap": "pareidolia", "spec-ready-gate0": "pareidolia", "merge-and-migrate": "spindle",
    "sundown": "mishmash", "mug": "alex-roth", "ios-sprint-resume": "plant-stager", "rebrand-r6": "zs-agency-site",
    "nutrition-companion": "rufus-sewell", "2026-11-10-session-handoff": "hold", "list-view-watcher": "alex-roth",
}

items = []  # (path, hint slug)
def _only_months(d):
    kids = [c for c in os.scandir(str(d)) if not ip.ignored(c.name)]
    return bool(kids) and all(c.is_dir(follow_symlinks=False) and ip.MONTH_RE.match(c.name) for c in kids)

def visit(d, hint):
    for e in sorted(os.scandir(str(d)), key=lambda e: e.name):
        if ip.ignored(e.name):
            continue
        p = Path(e.path)
        if e.is_symlink():
            print("skip symlink", p)
            continue
        if e.is_dir(follow_symlinks=False):
            if ip.MONTH_RE.match(e.name):
                visit(p, hint)
            elif e.name in ip.slugs() or _only_months(p):  # container; anything else is a bundle
                visit(p, e.name)
            else:
                items.append((p, hint))  # bundle: moves whole
        else:
            items.append((p, hint))
visit(SRC, None)

rows, used, empties = [], set(), []
for p, hint in items:
    f = p.name
    if p.is_file() and p.stat().st_size == 0:
        empties.append(p)
        continue
    slug = hint if hint in ip.slugs() and hint != ip.UNASSIGNED else ip.UNASSIGNED
    if slug == ip.UNASSIGNED:
        for key, val in OVERRIDES.items():
            if key in str(p.relative_to(SRC)):
                slug = val
                break
    if slug == ip.UNASSIGNED and hint:
        slug = ip.resolve_project(hint)
    if slug == ip.UNASSIGNED:
        slug = ip.resolve_project(f)
    if slug == ip.UNASSIGNED and p.is_file():
        title, _ = rt.title_snippet(str(p))
        slug = ip.resolve_project(title or "")
    st = p.stat()
    day, datestr = find_date(f, st.st_mtime)
    if p.is_dir() or "." not in f:
        stem, ext = f, ""
    else:
        stem, ext = f.rsplit(".", 1)
    subj = subject_of(stem, datestr, slug)
    code = ip.project_code(slug)
    base = "-".join([day.isoformat()] + ([code] if code else []) + [subj])
    folder = DST / slug / day.strftime("%Y-%m")
    n = 1
    while True:
        name = base + ("" if n == 1 else "-%02d" % n) + ("." + ext.lower() if ext else "")
        if str(folder / name) not in used and not (folder / name).exists():
            break
        n += 1
    used.add(str(folder / name))
    rows.append({"src": str(p), "dst": str(folder / name), "project": slug, "mtime": st.st_mtime})
print("empty files to drop:", len(empties))

for r in rows:
    print("%-16s %s\n%16s -> %s" % (r["project"], os.path.relpath(r["src"], SRC), "", os.path.relpath(r["dst"], DST)))
print(len(rows), "files")
if not APPLY:
    sys.exit(0)
man = Path(__file__).resolve().parents[1] / "manifests" / ("handoffs-top-level-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
with man.open("w") as fh:
    for e in empties:
        os.remove(str(e))
        fh.write(json.dumps({"op": "delete-empty", "src": str(e)}) + "\n")
    for r in rows:
        Path(r["dst"]).parent.mkdir(parents=True, exist_ok=True)
        dst, n = Path(r["dst"]), 2
        while True:  # atomic: never replace a file another writer created after planning
            try:
                rt.rename_noreplace(r["src"], str(dst))
                break
            except FileExistsError:
                b = Path(r["dst"])
                dst = b.with_name("%s-%02d%s" % (b.stem, n, b.suffix)) if b.suffix else b.with_name("%s-%02d" % (b.name, n))
                n += 1
        r["dst"] = str(dst)
        os.utime(r["dst"], (r["mtime"], r["mtime"]))
        fh.write(json.dumps({"op": "move", "src": r["src"], "dst": r["dst"]}) + "\n")
# remove now-empty dirs (only Icon\r / .DS_Store left)
for root, dirs, files in os.walk(SRC, topdown=False):
    left = [f for f in os.listdir(root) if not (f == ".DS_Store" or f == ip.FINDER_ICON)]
    if not left:
        for f in os.listdir(root):
            os.remove(os.path.join(root, f))
        os.rmdir(root)
if not SRC.exists():
    os.symlink("../handoffs", str(SRC))
    print("symlinked", SRC, "-> ../handoffs")
else:
    print("WARN: %s not empty; left in place" % SRC)
print("manifest", man)
