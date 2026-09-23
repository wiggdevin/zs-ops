#!/usr/bin/env python3
"""
consolidate_memory.py — janitor for the shared zs-memory bus.

Backstop for what the write-policy's soft layers miss. Run on a schedule
(launchd, hourly) on the new machine. Three jobs:
  1. Flag/remove CODE-DUMPS that slipped into memory (policy: decisions, not code).
  2. Flag NEAR-DUPLICATES for review (mem0 has weak dedup).
  3. Flag likely CONFLICTS (high text similarity, opposite assertions) for the
     HUMAN to resolve — never auto-delete (silent last-write-wins corrupts truth).

Stdlib only (no deps). Talks to the mem0 REST API. DRY-RUN by default.

  python3 consolidate_memory.py                 # report only (safe)
  python3 consolidate_memory.py --prune-code     # actually delete code-dumps
  MEM0_API=http://localhost:8765 USER_ID=zero-fleet python3 consolidate_memory.py
"""
import os, re, sys, json, urllib.request, urllib.parse
from difflib import SequenceMatcher

API = os.environ.get("MEM0_API", "http://localhost:8765")
USER_ID = os.environ.get("USER_ID", "zero-fleet")
PRUNE_CODE = "--prune-code" in sys.argv


def _token():
    # zs-memory requires bearer auth (token lives in ZS Vault). Uses the SCOPED
    # `automation` token, not the primary: the primary bypasses scope checks and
    # can target any user_id including delete_all_memories, which is far more
    # authority than a scheduled consolidation pass needs.
    #
    # --prune-code needs delete, which this scope refuses (403). Run that mode
    # explicitly with the maintenance token:
    #   ZS_MEMORY_TOKEN="$(zsvault get zs_memory_maintenance_token)" \
    #     python3 consolidate_memory.py --prune-code
    t = os.environ.get("ZS_MEMORY_TOKEN")
    if t:
        return t
    import subprocess
    try:
        return subprocess.check_output(
            [os.path.expanduser("~/bin/zsvault"), "get", "zs_memory_token_automation"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


TOKEN = _token()

CODE_SIGNS = [
    r"```",                       # code fences
    r"\bdef\s+\w+\s*\(",          # python def
    r"\bfunction\s+\w+\s*\(",     # js function
    r"=>\s*[{(]",                 # arrow fn
    r"\bimport\s+[\w{]",          # import
    r";\s*$",                     # statement terminator (multiline)
    r"</?[a-zA-Z][^>]*>",         # html/jsx tags
]
CODE_RE = re.compile("|".join(CODE_SIGNS), re.MULTILINE)

# A SINGLE unambiguous construct = code (mirrors write_policy.py AUDIT-3).
STRONG_CODE_RE = re.compile("|".join([
    r"```", r"\bdef\s+\w+\s*\(", r"\bfunction\s+\w+\s*\(", r"=>\s*[{(]",
]), re.MULTILINE)

# Transcript/oversize dumps (mirrors write_policy.py 2026-06-10 additions).
ROLE_LINE_RE = re.compile(r"^(user|assistant|system|agent|human)\s*:", re.IGNORECASE)
MAX_CONTENT_CHARS = 15_000
MAX_ROLE_LINES = 8


def _req(method, path, body=None):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read() or "{}")


def list_memories():
    # zs-memory REST: GET /api/v1/memories/?user_id=...  (TRAILING SLASH — without it a
    # 307 redirect drops the body and urllib returns empty). Response is a paginated Page
    # {"items":[...],"total":N,...}; tolerate results/list shapes too.
    items, page = [], 1
    while True:
        q = urllib.parse.urlencode({"user_id": USER_ID, "size": 100, "page": page})
        try:
            out = _req("GET", f"/api/v1/memories/?{q}")
        except Exception as e:
            print(f"✗ could not list memories: {e}", file=sys.stderr); sys.exit(2)
        batch = (out.get("items") or out.get("results") or []) if isinstance(out, dict) else out
        items.extend(batch)
        if not isinstance(out, dict) or page >= out.get("pages", 1) or not batch:
            return items
        page += 1


def looks_like_code(text):
    if not text:
        return False
    if STRONG_CODE_RE.search(text):
        return True
    hits = len(CODE_RE.findall(text))
    mono_lines = sum(1 for ln in text.splitlines() if ln.startswith(("    ", "\t")))
    return hits >= 2 or mono_lines >= 3


def looks_like_dump(text):
    if len(text) > MAX_CONTENT_CHARS:
        return "oversized"
    role_lines = sum(1 for ln in text.splitlines() if ROLE_LINE_RE.match(ln.strip()))
    if role_lines > MAX_ROLE_LINES:
        return "transcript"
    return None


def similar(a, b):
    return SequenceMatcher(None, a or "", b or "").ratio()


def main():
    mems = list_memories()
    print(f"Scanning {len(mems)} memories (user_id={USER_ID})  API={API}\n")

    code_dumps, raw_dumps, dups, conflicts = [], [], [], []
    for m in mems:
        text = m.get("memory") or m.get("content") or ""
        mid = m.get("id") or m.get("memory_id")
        if looks_like_code(text):
            code_dumps.append((mid, text[:120]))
        kind = looks_like_dump(text)
        if kind:
            raw_dumps.append((mid, kind, text[:120]))

    # pairwise near-dup / conflict (O(n^2); fine for a personal store)
    NEG = re.compile(r"\b(not|no longer|don'?t|never|deprecated|removed|reverted)\b", re.I)
    for i in range(len(mems)):
        for j in range(i + 1, len(mems)):
            ti = mems[i].get("memory") or mems[i].get("content") or ""
            tj = mems[j].get("memory") or mems[j].get("content") or ""
            s = similar(ti, tj)
            if s >= 0.92:
                dups.append((mems[i].get("id"), mems[j].get("id"), round(s, 2)))
            elif s >= 0.70 and (bool(NEG.search(ti)) != bool(NEG.search(tj))):
                conflicts.append((ti[:80], tj[:80], round(s, 2)))

    print(f"── CODE-DUMPS (policy violation): {len(code_dumps)} ──")
    for mid, preview in code_dumps:
        print(f"  [{mid}] {preview!r}")
        if PRUNE_CODE and mid:
            try:
                # zs-memory delete is `DELETE /api/v1/memories/` with a body — the per-id
                # path `/api/v1/memories/{id}` returns 405. (required: memory_ids, user_id)
                _req("DELETE", "/api/v1/memories/", {"memory_ids": [mid], "user_id": USER_ID})
                print("     ↳ deleted")
            except Exception as e:
                print(f"     ↳ delete failed: {e}")

    print(f"\n── TRANSCRIPT/OVERSIZE DUMPS (review — predate the live gate): {len(raw_dumps)} ──")
    for mid, kind, preview in raw_dumps:
        print(f"  [{mid}] ({kind}) {preview!r}")

    print(f"\n── NEAR-DUPLICATES (review): {len(dups)} ──")
    for a, b, s in dups[:50]:
        print(f"  {s}  {a}  ≈  {b}")

    print(f"\n── LIKELY CONFLICTS (HUMAN resolve — not auto-deleted): {len(conflicts)} ──")
    for a, b, s in conflicts[:50]:
        print(f"  {s}\n    A: {a}\n    B: {b}")

    if not PRUNE_CODE and code_dumps:
        print("\n(Run with --prune-code to delete the code-dumps above.)")


if __name__ == "__main__":
    main()
