#!/usr/bin/python3
"""inbox_index.py - write ~/Inbox/index.md: newest 20 items per project, OKF-style list lines.

    * [Title](relative/path) - type, date, description

Titles come from a markdown heading (or bundle README) when the file name is not
sensitive/secret-looking; otherwise the name is used and no excerpt is read. Stdlib only.
"""
import datetime as _dt
import os
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


if __name__ == "__main__":
    print(write_index())
