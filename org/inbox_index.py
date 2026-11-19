#!/usr/bin/python3
"""inbox_index.py - write ~/Inbox/index.md: newest 20 items per project, OKF-style list lines.

    * [Title](relative/path) - type, date, description

Titles come from a markdown heading (or bundle README) when the file name is not
sensitive/secret-looking; otherwise the name is used and no excerpt is read. Stdlib only.
"""
import datetime as _dt
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import inbox_paths as ip  # noqa: E402
import org_router as rt  # noqa: E402

PER_PROJECT = 20


def describe(item):
    path = Path(item["path"])
    if rt.SECRET_NAME_RE.search(path.name) or rt.SENSITIVE_NAME_RE.search(path.name):
        return path.name, "restricted-looking name, excerpt not read"
    title, snippet = rt.title_snippet(path, limit=140)
    if title:
        rest = snippet[len(title):].strip(" #") if snippet.startswith(title) else snippet
        return title, (rest[:100] or ("bundle" if item["is_dir"] else "note"))
    return path.name, "bundle" if item["is_dir"] else (path.suffix.lstrip(".") or "file") + " file"


def build_index(now=None):
    now = now or time.time()
    lines = ["# Inbox index", "",
             "Generated %s. Newest 20 items per project; layout is `<type>/<project>/<YYYY-MM>/<item>`. "
             "Find more with `~/bin/inbox-find [--project P] [--type T] [query]`." %
             _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"), ""]
    items = ip.find_items(limit=0)
    by_project = {}
    for it in items:
        by_project.setdefault(it["project"], []).append(it)
    reg = {p["slug"]: p for p in ip.load_registry()["projects"]}
    order = [s for s in reg if s in by_project and s != ip.UNASSIGNED] + ([ip.UNASSIGNED] if ip.UNASSIGNED in by_project else [])
    for slug in sorted(order, key=lambda s: (s == ip.UNASSIGNED, -max(i["mtime"] for i in by_project[s]))):
        its = by_project[slug]
        types = {}
        for i in its:
            types[i["type"]] = types.get(i["type"], 0) + 1
        lines.append("## %s" % reg.get(slug, {}).get("name", slug))
        lines.append("")
        lines.append("%d items (%s)" % (len(its), ", ".join("%s %d" % kv for kv in sorted(types.items()))))
        lines.append("")
        for it in its[:PER_PROJECT]:
            title, desc = describe(it)
            rel = os.path.relpath(it["path"], str(ip.INBOX))
            day = _dt.datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d")
            title = title.replace("[", "(").replace("]", ")")
            lines.append("* [%s](%s) - %s, %s, %s" % (title, quote(rel), it["type"], day, desc.replace("\n", " ")))
        lines.append("")
    return "\n".join(lines)


def write_index(path=None, now=None):
    path = Path(path or (ip.INBOX / "index.md"))
    text = build_index(now)
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(text)
    os.replace(tmp, str(path))
    return path


HANDOFF_ROOT_FILES = {"INDEX.md", "AGENTS.md", "START-HERE.md"}


def build_handoffs_index(now=None):
    """~/Inbox/handoffs/INDEX.md: every handoff newest first, one section per month."""
    now = now or time.time()
    reg = {p["slug"]: p for p in ip.load_registry()["projects"]}
    root = ip.handoffs_dir()
    items = [i for i in ip.find_items(type="handoffs", limit=0)
             if not (Path(i["path"]).parent == root and i["name"] in HANDOFF_ROOT_FILES)]

    def day_of(it):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", it["name"])
        return m.group(1) if m else _dt.datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d")

    items.sort(key=lambda i: (day_of(i), i["mtime"]), reverse=True)
    counts = {}
    for it in items:
        counts[it["project"]] = counts.get(it["project"], 0) + 1
    lines = ["# Handoffs index", "",
             "Generated %s. %d handoffs, newest first. Rules: [AGENTS.md](AGENTS.md). "
             "Rebuild: `~/bin/handoff-index`." % (_dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"), len(items)),
             "", "## Latest per project", "", "| Code | Project | Latest | Count |", "|---|---|---|---|"]
    seen = set()
    for it in items:
        slug = it["project"]
        if slug in seen:
            continue
        seen.add(slug)
        rel = quote(os.path.relpath(it["path"], str(root)))
        lines.append("| %s | %s | [%s](%s) | %d |" % (reg.get(slug, {}).get("code", "") or "-",
                     reg.get(slug, {}).get("name", slug), day_of(it), rel, counts[slug]))
    month = None
    for it in items:
        m = day_of(it)[:7]
        if m != month:
            month = m
            lines += ["", "## %s" % _dt.datetime.strptime(m, "%Y-%m").strftime("%B %Y"), "",
                      "| Date | Code | Handoff |", "|---|---|---|"]
        title, _desc = describe(it)
        title = title.replace("|", "/").replace("[", "(").replace("]", ")")
        rel = quote(os.path.relpath(it["path"], str(root)))
        code = reg.get(it["project"], {}).get("code", "") or it["project"]
        lines.append("| %s | %s | [%s](%s) |" % (day_of(it), code, title, rel))
    lines.append("")
    return "\n".join(lines)


def write_handoffs_index(path=None, now=None):
    root = ip.handoffs_dir()
    if not root.is_dir():
        return None
    path = Path(path or (root / "INDEX.md"))
    text = build_handoffs_index(now)
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(text)
    os.replace(tmp, str(path))
    return path


if __name__ == "__main__":
    if "--handoffs" in sys.argv[1:]:
        print(write_handoffs_index())
    else:
        print(write_index())
        print(write_handoffs_index())
