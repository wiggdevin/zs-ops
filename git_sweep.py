#!/usr/bin/env python3
"""git_sweep.py — the git cleanup behind the Hermes "git-sweep" cron job.
Added 2026-10-29. Devin asked for a job that merges finished PRs and removes stale
worktrees, so agents' leftovers do not pile up. The rules live here; the cron job runs
this script with no agent and delivers its output verbatim (empty output = silent).
Reviewed by GPT-6.1 Sol before it went live; its findings shaped the guards below.

Worktrees (linked worktrees only, never a main checkout). One is removed when ALL hold:
  - not locked, not bare, its directory exists;
  - clean: `git status --porcelain --untracked-files=all` is empty, with the user's
    status.showUntrackedFiles setting overridden for the check and for the removal; no
    file is marked assume-unchanged or skip-worktree; no rebase, merge, cherry-pick,
    revert or bisect is paused in it;
  - landed: HEAD is on origin's default branch, or GitHub lists a MERGED PR containing
    HEAD (squash merges leave the commits off the default branch);
  - idle: no commit or checkout for IDLE_H hours, and no process has its
    working directory inside it (an lsof failure counts as busy);
  - every ignored file outside known build output (node_modules, dist, .next, ...) also
    exists, byte for byte, at the same path in the main checkout (.env files and keys
    are therefore never lost).
  Right before removal the HEAD, status, process and ignored-file checks run again. The
  HEAD is saved as refs/zs-git-sweep/<name> so it stays recoverable (no ref, no removal). Removal is `git worktree remove`
  without --force; the local branch is deleted with `git update-ref -d` pinned to that
  HEAD, so a branch that moved in between, or is checked out in another worktree, is kept. Missing worktree directories are
  cleared with `git worktree prune`. A worktree idle for ASK_DAYS but dirty or not
  landed is listed under "needs you" and never touched.

PRs (open, authored by the gh user, owners in OWNERS). One is squash-merged when ALL hold:
  - the repo is in `allow` in git_sweep.json, and no line of its local AGENTS.md /
    CLAUDE.md says "do not merge" or "open the PR and stop", except lines listed for it
    under `policy_reviewed` (read by hand: they are about something else; a changed or
    new line blocks again). An unreadable or invalid config, or an unreadable policy
    file, blocks merging; the policy is read again right before the merge;
  - ready for review (not a draft) and its body records a review by another model
    (a "Review" heading or line naming Sol, GPT, Argon, Gemini, Claude, Opus, Sonnet or
    Codex, with no unticked box, "not reviewed", "pending" or open BLOCK in it;
    "Self-review" does not count). Base is the default branch, no hold label,
    no changes requested;
  - GitHub says MERGEABLE and CLEAN, and every check passed or was skipped, with at least
    one pass (no checks = not merged);
  - the same head commit was already seen by a run at least SETTLE_H hours earlier
    (so a PR is merged on its second sighting, never while an agent is still pushing).
  Every gate is checked again right before the merge, which is pinned to the head
  commit (--match-head-commit). Only once GitHub reports the PR MERGED (not merely
  queued) is the remote branch deleted, with a push lease on that commit, so a branch
  that received a new push is kept; a fork's branch is never touched.
  Ready PRs that fail a rule and drafts idle for ASK_DAYS are listed under "needs you".

Usage:
  git_sweep.py run            act and print the report (the cron script)
  git_sweep.py --dry-run run  decide and report; no fetch, merge, removal or state write
Log: ~/Library/Logs/zs-git-sweep.jsonl. Off switch: ZS_GIT_SWEEP=0 (same as --dry-run).
Runs on /usr/bin/python3 (3.9), no third-party modules.
"""
import fcntl
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOTS = [HOME / "projects", HOME / ".claude", HOME / ".codex", HOME / ".config"]
SKIP_PARTS = {"node_modules", "third-party", "archive", "clones", ".cache"}
CONFIG = Path(__file__).resolve().parent / "git_sweep.json"
LOG = HOME / "Library/Logs/zs-git-sweep.jsonl"
LOG_CAP_BYTES = 2 * 1024 * 1024
STATE = HOME / ".local/state/zs-git-sweep/state.json"
LOCK = HOME / ".local/state/zs-git-sweep/lock"
OWNERS = ["wiggdevin", "ZeroSum-Solutions"]
IDLE_H = 24
ASK_DAYS = 7
SETTLE_H = 1
REMIND_DAYS = 3
MAX_IGNORED_FILES = 5000
HOLD_LABELS = {"hold", "on-hold", "do-not-merge", "do not merge", "wip", "blocked"}
NO_MERGE_TEXT = re.compile(
    r"\b(never|do not|don't|not|no)\s+(\w+\s+)?(auto-?)?merg|pr[- ]and[- ]stop|open (a |the )?prs?(,| and) (then )?stop|"
    r"stop at (the|an open) pr|ends at open prs|leave .{0,20}prs? open", re.I)
REVIEW_HEAD = re.compile(r"^\s*(#+\s*|[-*]\s*|\*\*)?(cross[- ]model |second[- ]model )?reviews?\b", re.I)
SELF_REVIEW = re.compile(r"^\s*(#+\s*|[-*]\s*|\*\*)?self[- ]review", re.I)
REVIEWER = re.compile(r"\b(sol|gpt|argon|gemini|claude|opus|sonnet|codex)\b", re.I)
# A review section with any of these is unfinished or negative, so it does not count.
REVIEW_OPEN = re.compile(
    r"\[ \]|\b(has|have|was|were|is|are)\s*(not|n't)\s+(yet\s+)?(been\s+)?(run|reviewed|reviewing|review)|"
    r"\bnot\s+(yet\s+)?(been\s+)?reviewed|\bno review\b|\breview\s+(is\s+)?(pending|running|in progress|failed)|"
    r"\bpending\s+review|\btodo\b|\btbd\b|\bunresolved\b|\bnot\s+(yet\s+)?fixed|\bopen\s+(block|warn)|"
    r"\b(block|warn)\w*\s+(findings?\s+)?(remain|still open|open|unfixed|outstanding)", re.I)
# Ignored paths that are build output or caches: rebuilt on demand, never compared.
REGEN = {"node_modules", ".next", ".nuxt", ".svelte-kit", ".turbo", ".vercel", ".output", "dist", "build",
         "out", "coverage", ".cache", ".parcel-cache", ".venv", "venv", "__pycache__", ".pytest_cache",
         ".mypy_cache", ".ruff_cache", "target", ".gradle", ".DS_Store", ".eslintcache", ".codegraph",
         "playwright-report", "test-results", ".expo", "Pods", "DerivedData", "next-env.d.ts"}
REGEN_SUFFIX = (".log", ".tsbuildinfo", ".pyc")
GIT = "/usr/bin/git"
GIT_SAFE = [GIT, "--no-optional-locks", "-c", "status.showUntrackedFiles=all"]  # no index refresh writes
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"


class ConfigError(Exception):
    pass


def sh(args, cwd=None, timeout=60, raw=False):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1"))
        return r.returncode, r.stdout if raw else r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 124, "", str(e)


def git(repo, *args, timeout=60, raw=False):
    return sh([*GIT_SAFE, "-C", str(repo), *args], timeout=timeout, raw=raw)


# ---- discovery -------------------------------------------------------------------------

def find_repos():
    """Main checkouts under ROOTS, one per git common dir."""
    seen, repos = set(), []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS and depth < 4]
            if ".git" not in dirnames and ".git" not in filenames:
                continue
            dirnames[:] = [d for d in dirnames if d != ".git"]
            rc, common, _ = git(dirpath, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if rc != 0 or common in seen:
                continue
            seen.add(common)
            rc, top, _ = git(Path(common).parent if common.endswith("/.git") else dirpath,
                             "rev-parse", "--show-toplevel")
            if rc == 0:
                repos.append(Path(top))
    return repos


def parse_worktrees(text):
    """`git worktree list --porcelain` -> list of dicts; the first entry is the main one."""
    out, cur = [], None
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val, "head": "", "branch": "", "locked": False, "prunable": False,
                   "bare": False, "detached": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
        elif key in ("locked", "prunable", "bare", "detached"):
            cur[key] = True
    return out


def repo_slug(repo):
    rc, url, _ = git(repo, "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(\.git)?/?$", url) if rc == 0 else None
    return ("%s/%s" % (m.group(1), m.group(2))).lower() if m else ""


def default_ref(repo):
    rc, ref, _ = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and ref:
        return ref
    for cand in ("origin/main", "origin/master"):
        if git(repo, "rev-parse", "--verify", "-q", cand)[0] == 0:
            return cand
    return ""


def process_cwds():
    """Working directories of this user's processes, or None when lsof fails."""
    rc, out, _ = sh(["/usr/sbin/lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fn"], timeout=30)
    cwds = {line[1:] for line in out.splitlines() if line.startswith("n/")}
    return cwds if rc == 0 and cwds else None


def is_busy(path, cwds):
    return cwds is None or any(c == path or c.startswith(path + "/") for c in cwds)


def last_activity(path, gitdir):
    """Newest of the HEAD commit time and the worktree's HEAD / reflog mtimes (epoch s). The index
    is left out: any `git status` (an editor, a prompt) rewrites it without real work."""
    times = []
    rc, ct, _ = git(path, "log", "-1", "--format=%ct")
    if rc == 0 and ct.isdigit():
        times.append(int(ct))
    for name in ("HEAD", "logs/HEAD"):
        try:
            times.append(int((Path(gitdir) / name).stat().st_mtime))
        except OSError:
            pass
    return max(times) if times else 0


def is_regen(rel):
    parts = rel.rstrip("/").split("/")
    return any(p in REGEN for p in parts) or rel.endswith(REGEN_SUFFIX)


def ignored_files_safe(wt, main):
    """True when every ignored file outside build output exists identically in the main checkout."""
    rc, out, _ = git(wt, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory",
                     timeout=120, raw=True)
    if rc != 0:
        return False
    count = 0
    for rel in filter(None, out.split("\0")):
        if is_regen(rel):
            continue
        top = Path(wt) / rel
        if not os.path.lexists(top):
            return False
        files = [top] if not rel.endswith("/") else (
            Path(d) / f for d, dirs, fs in os.walk(top) for f in fs
            if not is_regen(os.path.relpath(os.path.join(d, f), wt)))
        for f in files:
            count += 1
            if count > MAX_IGNORED_FILES:
                return False
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                twin = Path(main) / f.relative_to(wt)
                if not twin.is_file() or not filecmp.cmp(str(f), str(twin), shallow=False):
                    return False
            except OSError:
                return False
    return True


def in_merged_pr(slug, sha):
    """True when GitHub lists a MERGED PR that contains this commit."""
    if not slug or not sha:
        return False
    rc, out, _ = sh([GH, "api", "repos/%s/commits/%s/pulls" % (slug, sha), "--jq",
                     "[.[] | select(.merged_at != null)] | length"], timeout=60)
    return rc == 0 and out.isdigit() and int(out) > 0


PAUSED_OPS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
              "sequencer")


def live_state(path):
    """(HEAD sha, clean) read now; ('', False) when unreadable. Not clean also covers files
    hidden from status (assume-unchanged, skip-worktree) and a paused rebase, merge,
    cherry-pick, revert or bisect."""
    rc, head, _ = git(path, "rev-parse", "HEAD")
    rc2, status, _ = git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    rc3, flags, _ = git(path, "ls-files", "-v", timeout=120)
    rc4, gitdir, _ = git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    hidden = rc3 != 0 or any(line[:1].islower() or line[:1] == "S" for line in flags.splitlines())
    paused = rc4 != 0 or any(os.path.lexists(os.path.join(gitdir, n)) for n in PAUSED_OPS)
    return (head if rc == 0 else ""), (rc2 == 0 and not status and not hidden and not paused)


# ---- worktree rules (pure) -------------------------------------------------------------

def worktree_verdict(f, now):
    """f: facts about one linked worktree. -> ("remove" | "keep" | "ask", reason)."""
    idle_h = (now - f["activity"]) / 3600.0
    if f["locked"] or f["bare"]:
        return "keep", "locked"
    if f["busy"]:
        return "keep", "a process is working in it"
    stale = idle_h >= ASK_DAYS * 24
    if not f["clean"]:
        return ("ask", "uncommitted changes, idle %dd" % (idle_h // 24)) if stale else ("keep", "dirty")
    if not f["landed"]:
        return ("ask", "work not merged, idle %dd" % (idle_h // 24)) if stale else ("keep", "not merged")
    if idle_h < IDLE_H:
        return "keep", "active in the last %dh" % IDLE_H
    if not f["files_safe"]:
        return "ask", "merged, but holds ignored files the main checkout lacks (.env, keys, data)"
    return "remove", f["landed"]


def check_worktree(repo, slug, dref, w, cwds, now):
    path = w["path"]
    rc, gitdir, _ = git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    head, clean = live_state(path)
    landed = ""
    if head and head == w["head"]:
        if dref and git(repo, "merge-base", "--is-ancestor", head, dref)[0] == 0:
            landed = "merged into %s" % dref
        elif clean and in_merged_pr(slug, head):
            landed = "its PR was merged"
    facts = {"locked": w["locked"], "bare": w["bare"], "busy": is_busy(path, cwds), "clean": clean,
             "landed": landed, "activity": last_activity(path, gitdir) if rc == 0 else now,
             "files_safe": False}
    if facts["clean"] and landed and not facts["busy"]:
        facts["files_safe"] = ignored_files_safe(path, repo)
    return worktree_verdict(facts, now), landed


def remove_worktree(repo, w, landed):
    """Re-check, keep a recovery ref, remove without --force, delete the branch pinned to HEAD."""
    path = w["path"]
    head, clean = live_state(path)
    if head != w["head"] or not clean or is_busy(path, process_cwds()) or not ignored_files_safe(path, repo):
        return False, "changed since it was checked; kept"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(path)) or "wt"
    ref = "refs/zs-git-sweep/%s-%s" % (name, head[:8])
    git(repo, "update-ref", "-m", "git-sweep: %s" % path, ref, head)
    if git(repo, "rev-parse", "--verify", "-q", ref)[1] != head:
        return False, "could not save a recovery ref; kept"
    rc, _, err = git(repo, "worktree", "remove", path)
    if rc != 0:
        return False, "remove failed: %s" % err[:160]
    log({"action": "worktree_removed", "path": path, "branch": w["branch"], "head": head, "why": landed})
    if not w["branch"]:
        return True, ""
    rc, text, _ = git(repo, "worktree", "list", "--porcelain")
    if rc != 0 or any(o["branch"] == w["branch"] for o in parse_worktrees(text)):
        return True, "; branch kept (checked out elsewhere)"
    rc, _, _ = git(repo, "update-ref", "-d", "refs/heads/" + w["branch"], head)
    return True, "; branch deleted" if rc == 0 else "; branch kept (moved since)"


def sweep_worktrees(repos, dry, now):
    removed, asks, errors = [], [], []
    for repo in repos:
        try:
            rc, text, _ = git(repo, "worktree", "list", "--porcelain")
            linked = (parse_worktrees(text) if rc == 0 else [])[1:]
            if not linked:
                continue
            if any(w["prunable"] for w in linked) and not dry:
                git(repo, "worktree", "prune")
            live = [w for w in linked if not w["prunable"] and os.path.isdir(w["path"])]
            if not live:
                continue
            if not dry:
                git(repo, "fetch", "--prune", "--quiet", "origin", timeout=120)
            dref, slug, cwds = default_ref(repo), repo_slug(repo), process_cwds()
            for w in live:
                label = "%s (%s)" % (short(w["path"]), w["branch"] or "detached")
                try:
                    (verdict, why), landed = check_worktree(repo, slug, dref, w, cwds, now)
                    if verdict == "ask":
                        asks.append("worktree %s: %s" % (label, why))
                    if verdict != "remove":
                        continue
                    if dry:
                        removed.append("%s: %s (dry run)" % (label, why))
                        continue
                    ok, note = remove_worktree(repo, w, landed)
                    (removed if ok else errors).append("%s: %s%s" % (label, why if ok else "", note))
                except Exception as e:  # one bad worktree must not cost the report
                    errors.append("worktree %s: %s" % (label, e))
        except Exception as e:
            errors.append("repo %s: %s" % (short(repo), e))
    return removed, asks, errors


# ---- PR rules (pure) -------------------------------------------------------------------

def checks_passed(rollup):
    if not rollup:
        return False, "no CI checks"
    passed = 0
    for c in rollup:
        if c.get("__typename") == "StatusContext":
            state = (c.get("state") or "").upper()
            if state == "SUCCESS":
                passed += 1
                continue
            return False, "check %s is %s" % (c.get("context", "?"), state.lower() or "pending")
        if (c.get("status") or "").upper() != "COMPLETED":
            return False, "check %s still running" % c.get("name", "?")
        concl = (c.get("conclusion") or "").upper()
        if concl == "SUCCESS":
            passed += 1
        elif concl != "SKIPPED":
            return False, "check %s: %s" % (c.get("name", "?"), concl.lower() or "no result")
    return (True, "") if passed else (False, "no check passed")


def has_review(body):
    """True when the PR body records a review by a named model (not a self-review)."""
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if SELF_REVIEW.match(line) or not REVIEW_HEAD.match(line):
            continue
        section = [line]
        if line.lstrip().startswith("#"):
            for nxt in lines[i + 1:]:
                if nxt.lstrip().startswith("#"):
                    break
                section.append(nxt)
        text = "\n".join(section)
        if REVIEWER.search(text) and not REVIEW_OPEN.search(text):
            return True
    return False


def pr_verdict(pr, policy_block, default_branch, first_seen, now):
    """-> ("merge" | "ask" | "skip", reason). policy_block: why the repo forbids merging, or ''.
    first_seen: epoch when a run first saw this head commit (now on the first sighting)."""
    if pr.get("isDraft"):
        idle_d = (now - iso_ts(pr.get("updatedAt"))) / 86400.0
        return ("ask", "draft, idle %dd" % idle_d) if idle_d >= ASK_DAYS else ("skip", "draft")
    if policy_block:
        return "skip", policy_block
    if pr.get("baseRefName") != default_branch:
        return "skip", "stacked on %s" % pr.get("baseRefName")
    labels = {(lab.get("name") or "").lower() for lab in pr.get("labels") or []}
    if labels & HOLD_LABELS:
        return "skip", "on hold (label)"
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return "ask", "changes requested"
    if not has_review(pr.get("body")):
        return "ask", "ready, but the PR body records no review by another model"
    ok, why = checks_passed(pr.get("statusCheckRollup"))
    if not ok:
        return "ask", why
    if pr.get("mergeable") != "MERGEABLE":
        return "ask", "merge conflict" if pr.get("mergeable") == "CONFLICTING" else "mergeability unknown"
    if pr.get("mergeStateStatus") != "CLEAN":
        return "ask", "GitHub merge state %s" % (pr.get("mergeStateStatus") or "unknown").lower()
    if now - first_seen < SETTLE_H * 3600:
        return "skip", "head commit not yet seen by an earlier run"
    return "merge", ""


def repo_policy(slug, local_paths, cfg):
    if slug not in cfg["allow"]:
        return "not in allow in git_sweep.json"
    if not local_paths:
        return "no local clone to read its merge policy"
    reviewed = cfg["policy_reviewed"].get(slug, set())
    for p in local_paths:
        for name in ("AGENTS.md", "CLAUDE.md"):
            try:
                text = (p / name).read_text(errors="ignore")
            except FileNotFoundError:
                continue
            except OSError:
                return "cannot read its %s" % name
            for line in text.splitlines():
                if NO_MERGE_TEXT.search(line) and line.strip() not in reviewed:
                    return "its %s says not to merge: %s" % (name, line.strip()[:80])
    return ""


PR_FIELDS = ("number,title,body,url,state,isDraft,updatedAt,baseRefName,headRefName,headRefOid,mergeable,"
             "mergeStateStatus,reviewDecision,labels,statusCheckRollup,isCrossRepository")


def view_pr(slug, num):
    rc, out, err = sh([GH, "pr", "view", str(num), "-R", slug, "--json", PR_FIELDS], timeout=60)
    if rc != 0:
        raise RuntimeError("read failed: %s" % err[:120])
    return json.loads(out)


def sweep_prs(repos, dry, now, cfg, seen):
    """seen: {"slug#num": [head sha, first seen epoch]}; updated in place."""
    local = {}
    for r in repos:
        slug = repo_slug(r)
        if slug:
            local.setdefault(slug, []).append(r)
    args = [GH, "search", "prs", "--author", "@me", "--state", "open", "--limit", "100",
            "--json", "repository,number"]
    for o in OWNERS:
        args += ["--owner", o]
    rc, out, err = sh(args, timeout=90)
    if rc != 0:
        return [], [], ["PR search failed: %s" % err[:160]]
    merged, asks, errors, defaults, still_open = [], [], [], {}, set()
    for hit in json.loads(out or "[]"):
        slug, num = hit["repository"]["nameWithOwner"], hit["number"]
        key = "%s#%d" % (slug, num)
        still_open.add(key)
        try:
            pr = view_pr(slug, num)
            if slug not in defaults:
                rc, d, _ = sh([GH, "repo", "view", slug, "--json", "defaultBranchRef", "-q",
                               ".defaultBranchRef.name"], timeout=30)
                defaults[slug] = d if rc == 0 else ""
            prev = seen.get(key)
            first = prev[1] if prev and prev[0] == pr["headRefOid"] else now
            if not dry:
                seen[key] = [pr["headRefOid"], first]
            paths = local.get(slug.lower(), [])
            policy = repo_policy(slug.lower(), paths, cfg)
            verdict, why = pr_verdict(pr, policy, defaults[slug], first, now)
            label = "%s %s" % (key, pr.get("title", "")[:60])
            if verdict == "ask":
                asks.append("PR %s: %s" % (label, why))
            if verdict != "merge":
                continue
            if dry:
                merged.append(label + " (dry run)")
                continue
            again = view_pr(slug, num)  # every gate again, as close to the merge as possible
            policy = repo_policy(slug.lower(), paths, load_config())
            if again["headRefOid"] != pr["headRefOid"] or pr_verdict(again, policy, defaults[slug], first, now)[0] != "merge":
                asks.append("PR %s: changed during the sweep; not merged" % label)
                continue
            rc, _, err = sh([GH, "pr", "merge", str(num), "-R", slug, "--squash",
                             "--match-head-commit", pr["headRefOid"]], cwd=str(HOME), timeout=120)
            if rc != 0:
                errors.append("PR %s: merge failed: %s" % (label, err[:160]))
                continue
            if view_pr(slug, num).get("state") != "MERGED":
                asks.append("PR %s: handed to GitHub (merge queue or pending); branch kept" % label)
                log({"action": "pr_queued", "repo": slug, "number": num, "head": pr["headRefOid"]})
                continue
            log({"action": "pr_merged", "repo": slug, "number": num, "head": pr["headRefOid"]})
            note = "" if not pr.get("isCrossRepository") else " (fork branch kept)"
            if not note:
                note = delete_remote_branch(paths[0], pr["headRefName"], pr["headRefOid"])
            merged.append(label + note)
        except Exception as e:  # one bad PR must not cost the report
            errors.append("PR %s: %s" % (key, e))
    for key in [k for k in seen if k not in still_open]:
        del seen[key]
    return merged, asks, errors


def delete_remote_branch(repo, branch, sha):
    """Delete origin's branch only if it still points at the merged head commit."""
    rc, _, _ = git(repo, "push", "--quiet", "--force-with-lease=refs/heads/%s:%s" % (branch, sha),
                   "origin", ":refs/heads/%s" % branch, timeout=60)
    return "" if rc == 0 else " (remote branch kept: it moved or is protected)"


# ---- report ----------------------------------------------------------------------------

def iso_ts(text):
    try:
        return datetime.strptime((text or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def short(path):
    return str(path).replace(str(HOME), "~", 1)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def load_config():
    """{"allow": set, "policy_reviewed": {slug: set of lines}}. Raises ConfigError when the file is bad."""
    try:
        raw = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        raise ConfigError("git_sweep.json unreadable: %s" % e)
    if not isinstance(raw, dict):
        raise ConfigError("git_sweep.json is not an object")
    allow = raw.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(s, str) for s in allow):
        raise ConfigError("git_sweep.json: allow must be a list of owner/repo strings")
    reviewed = raw.get("policy_reviewed", {})
    if not isinstance(reviewed, dict) or not all(
            isinstance(v, list) and all(isinstance(x, str) for x in v) for v in reviewed.values()):
        raise ConfigError("git_sweep.json: policy_reviewed must map owner/repo to a list of lines")
    return {"allow": {s.lower() for s in allow},
            "policy_reviewed": {k.lower(): {x.strip() for x in v} for k, v in reviewed.items()}}


def log(rec):
    """Best effort: a full disk must not cost the report."""
    rec = dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if LOG.stat().st_size > LOG_CAP_BYTES:
            lines = LOG.read_text().splitlines(keepends=True)
            tmp = LOG.with_name(LOG.name + ".tmp")
            tmp.write_text("".join(lines[len(lines) // 2:]))
            os.replace(tmp, LOG)
    except OSError as e:
        print("(log write failed: %s)" % e.strerror, file=sys.stderr)


def render(merged, removed, asks, errors, show_asks, dry):
    lines = []
    for title, items in (("merged PRs", merged), ("removed worktrees", removed), ("errors", errors)):
        if items:
            lines += ["## %s (%d)" % (title, len(items))] + ["- " + i for i in items] + [""]
    if show_asks and asks:
        lines += ["## needs you (%d)" % len(asks)] + ["- " + i for i in sorted(asks)] + [""]
    if lines:
        head = "# git sweep %s%s" % (time.strftime("%Y-%m-%d %H:%M"), " (dry run)" if dry else "")
        lines = [head, ""] + lines
    return "\n".join(lines).rstrip()


def run(dry):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    now = time.time()
    state = load_json(STATE, {})
    if not isinstance(state, dict):
        state = {}
    seen = state.get("pr_heads") if isinstance(state.get("pr_heads"), dict) else {}
    seen = {k: v for k, v in seen.items() if isinstance(v, list) and len(v) == 2
            and isinstance(v[0], str) and isinstance(v[1], (int, float))}
    if not isinstance(state.get("asks_at"), (int, float)):
        state["asks_at"] = 0
    merged, removed, asks, errors = [], [], [], []
    try:
        repos = find_repos()
        try:
            m, a, e = sweep_prs(repos, dry, now, load_config(), seen)
            merged, asks, errors = m, asks + a, errors + e
        except ConfigError as e:
            errors.append("PR merging off this run: %s" % e)
        r, a, e = sweep_worktrees(repos, dry, now)
        removed, asks, errors = r, asks + a, errors + e
    except Exception as e:
        errors.append("sweep aborted: %r" % e)
    finally:
        digest = hashlib.sha256("\n".join(sorted(asks)).encode()).hexdigest()
        show_asks = bool(asks) and (digest != state.get("asks") or now - state.get("asks_at", 0) >= REMIND_DAYS * 86400)
        text = render(merged, removed, asks, errors, show_asks, dry)
        if text:
            print(text)
        if not dry:
            state["pr_heads"] = seen
            if show_asks:
                state.update(asks=digest, asks_at=now)
            try:
                tmp = STATE.with_name(STATE.name + ".tmp")
                tmp.write_text(json.dumps(state))
                os.replace(tmp, STATE)
            except OSError as e:
                print("(state write failed: %s)" % e.strerror, file=sys.stderr)
        log({"action": "run", "dry": dry, "merged": len(merged), "removed": len(removed),
             "asks": len(asks), "errors": len(errors)})
    return 0


def main(argv):
    dry = "--dry-run" in argv or os.environ.get("ZS_GIT_SWEEP") == "0"
    args = [a for a in argv if a != "--dry-run"]
    if not args or args[0] == "run":
        return run(dry)
    print(__doc__.split("Usage:")[1].split("Log:")[0].rstrip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
