#!/usr/bin/python3
"""inbox_paths.py - the Inbox write contract and reader (stdlib only, python 3.9 ok).

Layout (spec: ~/.config/zs-ops/org/EXECUTION-SPEC.md):
    ~/Inbox/<type>/<project>/<YYYY-MM>/<YYYY-MM-DD>-<project>-<subject>[.ext]
    types: html plans pdfs screenshots notes misc; "handoffs" maps to notes/handoffs.

Python:
    from inbox_paths import resolve_project, allocate, find, find_items
    resolve_project("mishh-mashh-2 tidy")      -> "mishmash"
    allocate("mm", "plans", "Source Kits", ext="md") -> Path (parent created, collision-free)
    find(project="sasha", query="audit")       -> [Path, ...] newest first (new + legacy flat)

CLI (wrappers: ~/bin/inbox-path, ~/bin/inbox-find):
    inbox_paths.py path <project-or-alias> <type|handoffs> <subject> [--ext md] [--bundle]
                        [--date YYYY-MM-DD] [--json]
    inbox_paths.py find [--project X] [--type T] [--latest] [--limit N] [--long] [query ...]

Env: ZS_INBOX_ROOT (default ~/Inbox), ZS_ORG_REGISTRY (default org/projects.json next to
this file). Unknown projects resolve to "_unassigned".
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
INBOX = Path(os.environ.get("ZS_INBOX_ROOT") or (Path.home() / "Inbox"))
REGISTRY = Path(os.environ.get("ZS_ORG_REGISTRY") or (HERE / "projects.json"))
UNASSIGNED = "_unassigned"
DEFAULT_TYPES = ["html", "plans", "pdfs", "screenshots", "notes", "misc"]
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHORT_ALIAS_LEN = 2  # aliases this short match only as the first token (or whole text)
MAX_SUBJECT = 64
FINDER_ICON = "Icon\r"  # custom-icon file Finder keeps in iconed folders

_registry_cache = {}


# ----------------------------------------------------------------- registry
def load_registry(path=None):
    path = Path(path) if path else REGISTRY
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = _registry_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    with path.open() as fh:
        data = json.load(fh)
    _registry_cache[key] = (mtime, data)
    return data


def slugs():
    return [p["slug"] for p in load_registry()["projects"]]


def inbox_types():
    return list(load_registry().get("inbox_types") or DEFAULT_TYPES)


def admin_names():
    return set(load_registry().get("inbox_admin") or [])


def ignored(name):
    """Names that are never items: dotfiles and Finder's Icon\\r custom-icon file."""
    return name.startswith(".") or name == FINDER_ICON


def _norm(text):
    """Lowercase, separators (space _ - . / etc.) collapsed to one hyphen."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_alias_index_cache = {}


def _alias_index():
    """[(normalized alias, slug, compiled regex, is_weak)] sorted longest first."""
    reg = load_registry()
    key = id(reg)
    hit = _alias_index_cache.get(key)
    if hit:
        return hit
    seen = {}
    for proj in reg["projects"]:
        slug = proj["slug"]
        if slug == UNASSIGNED:
            continue
        for alias in list(proj.get("aliases") or []) + [slug, proj.get("name", "")]:
            norm = _norm(alias)
            if norm and norm not in seen:
                seen[norm] = slug
    weak = {_norm(a) for a in reg.get("weak_aliases", [])}
    out = []
    for norm, slug in seen.items():
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")
        out.append((norm, slug, rx, norm in weak))
    out.sort(key=lambda t: (-len(t[0]), t[0]))
    _alias_index_cache.clear()
    _alias_index_cache[key] = out
    return out


def resolve_project(text):
    """Slug for free text by alias match; "_unassigned" if nothing matches.

    Normalizes both sides to lowercase hyphenated tokens, matches aliases on word
    boundaries ("be" does not match "bench"). The longest alias wins; ties go to the
    earliest position. Aliases of <=2 chars ("be", "mm", "hf") only count when they are
    the first token or the whole text, so prose like "to-be-done" stays unassigned.
    The date prefix is ignored and a match at the start of the name (the project token in
    "<date>-<project>-<subject>") beats a longer alias later in it.
    Registry "weak_aliases" (generic words such as "design", "system") only decide when no
    strong alias matched, so "sasha-gemini-design-audit" is sasha, not design-research.
    """
    norm = _norm(text or "")
    norm = re.sub(r"^(\d{4}-\d{2}-\d{2}|\d{8})-", "", norm)  # <date>-<project>-<subject> convention
    if not norm:
        return UNASSIGNED
    best = None  # (strong, anchored, len, -pos, slug): strong beats weak, a leading match beats a later one
    for alias, slug, rx, is_weak in _alias_index():
        m = rx.search(norm)
        if not m:
            continue
        if len(alias) <= SHORT_ALIAS_LEN and m.start() != 0:
            continue
        cand = (0 if is_weak else 1, 1 if m.start() == 0 else 0, len(alias), -m.start(), slug)
        if best is None or cand > best:
            best = cand
    return best[4] if best else UNASSIGNED


# ----------------------------------------------------------------- allocate
def slugify(text, limit=MAX_SUBJECT):
    s = _norm(text)
    if len(s) > limit:
        cut = s[:limit]
        if "-" in cut and s[limit] != "-":
            cut = cut.rsplit("-", 1)[0] or cut
        s = cut.strip("-")
    return s or "item"


def _as_date(value):
    if value is None:
        return _dt.date.today()
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.datetime.strptime(str(value), "%Y-%m-%d").date()


def type_dir(type_name):
    t = str(type_name).strip().lower()
    if t == "handoffs":
        return INBOX / "notes" / "handoffs"
    if t not in inbox_types():
        raise ValueError("unknown inbox type %r (use %s or handoffs)" % (type_name, ", ".join(inbox_types())))
    return INBOX / t


def allocate(project, type, subject, ext=None, bundle=False, date=None):
    """Return an absolute collision-free Path and create its parent directory.

    <Inbox>/<type>/<slug>/<YYYY-MM>/<YYYY-MM-DD>-<slug>-<subject>[.ext]; for the
    "_unassigned" project the slug is left out of the name. A bundle is a directory:
    it is created (atomic mkdir) so the name stays reserved. A file is not created.
    Same-day collisions get -02, -03 ...
    """
    slug = project if project in slugs() else resolve_project(project)
    day = _as_date(date)
    base = type_dir(type) / slug / day.strftime("%Y-%m")
    subj = slugify(subject)
    parts = [day.isoformat()]
    if slug != UNASSIGNED and not (subj == slug or subj.startswith(slug + "-")):
        parts.append(slug)
    parts.append(subj)
    stem = "-".join(parts)
    suffix = ""
    if ext and not bundle:
        e = str(ext).lstrip(".").lower()
        if not re.fullmatch(r"[a-z0-9]+(\.[a-z0-9]+)?", e):
            raise ValueError("bad extension %r" % ext)
        suffix = "." + e
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = stem + ("" if n == 1 else "-%02d" % n)
        cand = base / (name + suffix)
        if bundle:
            try:
                cand.mkdir()
                return cand
            except FileExistsError:
                n += 1
                continue
        if not os.path.lexists(str(cand)):
            return cand
        n += 1


# --------------------------------------------------------------------- find
def _project_dirs(tdir):
    """Registered project directories directly inside a type dir."""
    out = []
    for slug in slugs():
        d = tdir / slug
        if d.is_dir() and not d.is_symlink():
            out.append(d)
    return out


def _entries(d):
    try:
        return [e for e in os.scandir(str(d))]
    except OSError:
        return []


def _item(path, project, tname, month, st):
    return {"path": str(path), "name": path.name, "project": project, "type": tname,
            "month": month, "mtime": st.st_mtime, "is_dir": os.path.isdir(str(path))}


def _scan_type(tname):
    """Every item for a type: new layout items + legacy flat items."""
    tdir = type_dir(tname)
    items = []
    if not tdir.is_dir():
        return items
    slug_set = set(slugs())
    for e in _entries(tdir):
        name = e.name
        if ignored(name):
            continue
        p = Path(e.path)
        is_proj = name in slug_set and e.is_dir(follow_symlinks=False)
        if is_proj:
            for m in _entries(p):
                if ignored(m.name) or not MONTH_RE.match(m.name) or not m.is_dir():
                    continue
                for it in _entries(m.path):
                    if ignored(it.name):
                        continue
                    try:
                        st = it.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    items.append(_item(Path(it.path), name, tname, m.name, st))
            continue
        # legacy flat item (or admin/month dir, which we do not list)
        if name in admin_names() or MONTH_RE.match(name) or name == "handoffs":
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        items.append(_item(p, resolve_project(name), tname, None, st))
    return items


def find_items(project=None, type=None, query=None, limit=20):
    """Items newest first, as dicts: path name project type month mtime is_dir."""
    want = None
    if project:
        want = project if project in slugs() else resolve_project(project)
    types = [type] if type else inbox_types() + ["handoffs"]
    terms = [t for t in re.split(r"\s+", (query or "").lower().strip()) if t]
    out = []
    for tname in types:
        for it in _scan_type(tname):
            if want and it["project"] != want:
                continue
            if terms:
                hay = (it["name"] + " " + it["project"]).lower()
                nhay = _norm(it["name"] + " " + it["project"])
                if not all(t in hay or _norm(t) in nhay for t in terms):
                    continue
            out.append(it)
    out.sort(key=lambda i: (-i["mtime"], i["path"]))
    return out[: max(0, int(limit))] if limit else out


def find(project=None, type=None, query=None, limit=20):
    """Paths (pathlib.Path) newest first over the new layout and legacy flat paths."""
    return [Path(i["path"]) for i in find_items(project, type, query, limit)]


# ---------------------------------------------------------------------- cli
def _cmd_path(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="inbox-path",
                                 description="Print a collision-free Inbox path and create its parent dir.")
    ap.add_argument("project", help="project slug or alias; unknown -> _unassigned")
    ap.add_argument("type", help="html|plans|pdfs|screenshots|notes|misc|handoffs")
    ap.add_argument("subject")
    ap.add_argument("--ext")
    ap.add_argument("--bundle", action="store_true", help="directory item (created)")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--kind", help="accepted for forward compatibility; unused")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        slug = a.project if a.project in slugs() else resolve_project(a.project)
        p = allocate(slug, a.type, a.subject, ext=a.ext, bundle=a.bundle, date=a.date)
    except (ValueError, OSError) as exc:
        print("inbox-path: %s" % exc, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"path": str(p), "project": slug, "type": a.type, "bundle": a.bundle}))
    else:
        print(p)
    return 0


def _cmd_find(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="inbox-find",
                                 description="List Inbox items newest first (new layout + legacy flat).")
    ap.add_argument("--project")
    ap.add_argument("--type")
    ap.add_argument("--latest", action="store_true", help="only the newest match")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--long", action="store_true", help="date, project, type before the path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--format", choices=["path", "long", "json"])
    ap.add_argument("query", nargs="*")
    a = ap.parse_args(argv)
    limit = 1 if a.latest else a.limit
    try:
        rows = find_items(a.project, a.type, " ".join(a.query), limit)
    except ValueError as exc:
        print("inbox-find: %s" % exc, file=sys.stderr)
        return 2
    fmt = a.format or ("json" if a.json else "long" if a.long else "path")
    if fmt == "json":
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            if fmt == "long":
                when = _dt.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
                print("%s  %-14s %-11s %s" % (when, r["project"], r["type"], r["path"]))
            else:
                print(r["path"])
    return 0 if rows else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("path", "find"):
        print(__doc__, file=sys.stderr)
        return 2
    return _cmd_path(argv[1:]) if argv[0] == "path" else _cmd_find(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
