"""git_sweep: the pure worktree and PR rules on stubbed facts. No git or gh calls. Python 3.9+."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_sweep as gs  # noqa: E402

H = 3600
NOW = 1_800_000_000.0


def wt(**kw):
    f = {"locked": False, "bare": False, "busy": False, "clean": True, "landed": "merged into origin/main",
         "activity": NOW - 48 * H, "files_safe": True}
    f.update(kw)
    return f


def test_worktree_removed_only_when_clean_landed_idle():
    assert gs.worktree_verdict(wt(), NOW)[0] == "remove"
    assert gs.worktree_verdict(wt(activity=NOW - 2 * H), NOW)[0] == "keep"
    assert gs.worktree_verdict(wt(busy=True), NOW)[0] == "keep"
    assert gs.worktree_verdict(wt(locked=True), NOW)[0] == "keep"
    assert gs.worktree_verdict(wt(clean=False), NOW)[0] == "keep"
    assert gs.worktree_verdict(wt(landed=""), NOW)[0] == "keep"


def test_stale_unfinished_worktree_is_asked_never_removed():
    old = NOW - 8 * 24 * H
    assert gs.worktree_verdict(wt(clean=False, activity=old), NOW)[0] == "ask"
    assert gs.worktree_verdict(wt(landed="", activity=old), NOW)[0] == "ask"
    assert gs.worktree_verdict(wt(files_safe=False), NOW)[0] == "ask"
    assert gs.worktree_verdict(wt(busy=True, clean=False, activity=old), NOW)[0] == "keep"


def test_parse_worktrees():
    text = ("worktree /r\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /r/wt\nHEAD bbb\ndetached\nlocked reason\n\n"
            "worktree /gone\nHEAD ccc\nbranch refs/heads/x\nprunable gitdir file points to non-existent location\n")
    a, b, c = gs.parse_worktrees(text)
    assert (a["path"], a["branch"]) == ("/r", "main")
    assert b["detached"] and b["locked"] and b["branch"] == ""
    assert c["prunable"] and c["branch"] == "x"


def run_check(name, conclusion, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def pr(**kw):
    p = {"isDraft": False, "baseRefName": "main", "labels": [], "reviewDecision": "",
         "statusCheckRollup": [run_check("ci", "SUCCESS"), run_check("lint", "SKIPPED")],
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "updatedAt": "2027-01-15T00:00:00Z",
         "body": "Summary.\n\n## Review\nGPT-6.1 Sol xhigh: no findings.\n"}
    p.update(kw)
    return p


SEEN = NOW - 12 * H


def verdict(p, policy="", first=SEEN):
    return gs.pr_verdict(p, policy, "main", first, NOW)[0]


def test_pr_merged_only_when_every_gate_holds():
    assert verdict(pr()) == "merge"
    assert verdict(pr(), policy="not in allow") == "skip"
    assert verdict(pr(isDraft=True)) == "skip"
    assert verdict(pr(baseRefName="feat/x")) == "skip"
    assert verdict(pr(labels=[{"name": "Hold"}])) == "skip"
    assert verdict(pr(reviewDecision="CHANGES_REQUESTED")) == "ask"
    assert verdict(pr(mergeable="CONFLICTING")) == "ask"
    assert verdict(pr(mergeStateStatus="BLOCKED")) == "ask"
    assert verdict(pr(mergeStateStatus="UNSTABLE")) == "ask"


def test_pr_waits_for_a_second_sighting_of_the_same_head():
    assert verdict(pr(), first=NOW) == "skip"
    assert verdict(pr(), first=NOW - 0.5 * H) == "skip"


def test_review_evidence():
    assert gs.has_review("- Review: GPT-6.1 Sol xhigh, no findings.")
    assert gs.has_review("## Review\n\nGPT-6.1 Sol and Gemini 4 Argon reviewed the branch.\n## Test plan\n")
    assert gs.has_review("**Cross-model review:** Claude Opus found nothing.")
    assert not gs.has_review("## Self-review\n- DeepSeek failed; Claude checked it.")
    assert not gs.has_review("## Review\nTODO\n## Notes\nSol was offline.")
    assert not gs.has_review("Ready for review.")
    assert not gs.has_review(None)
    assert not gs.has_review("## Review\n- [ ] GPT-6.1 Sol review")
    assert not gs.has_review("Review: Claude has NOT reviewed this PR yet.")
    assert not gs.has_review("## Review\nSol found one BLOCK; the BLOCK remains open.")
    assert gs.has_review("## Review\nGPT-6.1 Sol and Gemini 4 Argon reviewed the branch. All verified BLOCK and "
                         "WARN findings are fixed: the stall that blocked the whole pass, cached failures.")
    assert verdict(pr(body="## Summary\nFix.")) == "ask"


def test_checks_fail_closed():
    assert not gs.checks_passed([])[0]
    assert not gs.checks_passed([run_check("lint", "SKIPPED")])[0]
    assert not gs.checks_passed([run_check("ci", "SUCCESS"), run_check("e2e", "FAILURE")])[0]
    assert not gs.checks_passed([run_check("ci", "", status="IN_PROGRESS")])[0]
    assert not gs.checks_passed([run_check("ci", "CANCELLED")])[0]
    assert not gs.checks_passed([{"__typename": "StatusContext", "context": "x", "state": "PENDING"}])[0]
    assert gs.checks_passed([{"__typename": "StatusContext", "context": "x", "state": "SUCCESS"}])[0]


def test_stale_draft_is_asked():
    assert verdict(pr(isDraft=True, updatedAt="2027-01-01T00:00:00Z")) == "ask"


def test_repo_policy(tmp_path):
    cfg = {"allow": {"o/plain", "o/strict", "o/reviewed", "o/none"},
           "policy_reviewed": {"o/reviewed": {"- Never merge. The hub lands PRs."}}}
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "AGENTS.md").write_text("Land with zs-land.\n")
    strict = tmp_path / "strict"
    strict.mkdir()
    (strict / "CLAUDE.md").write_text("- Never merge. The hub lands PRs.\n")
    assert gs.repo_policy("o/plain", [plain], cfg) == ""
    assert gs.repo_policy("o/strict", [strict], cfg)
    assert gs.repo_policy("o/reviewed", [strict], cfg) == ""
    (strict / "CLAUDE.md").write_text("- Never merge. The hub lands PRs.\n- Do not merge on Fridays.\n")
    assert gs.repo_policy("o/reviewed", [strict], cfg), "a new prohibition must block again"
    assert gs.repo_policy("o/other", [plain], cfg)
    assert gs.repo_policy("o/none", [], cfg)
    for text in ("Open a PR and stop.", "Do not automatically merge PRs.", "It will not auto-merge.",
                 "Agents stop at the PR."):
        (strict / "CLAUDE.md").write_text(text)
        assert gs.repo_policy("o/strict", [strict], cfg), text


def test_bad_config_turns_merging_off(tmp_path):
    gs.CONFIG = tmp_path / "git_sweep.json"
    assert gs.load_config() == {"allow": set(), "policy_reviewed": {}}
    gs.CONFIG.write_text('{"allow": ["O/R"]')
    try:
        gs.load_config()
        raise AssertionError("truncated config accepted")
    except gs.ConfigError:
        pass
    gs.CONFIG.write_text('{"allow": "O/R"}')
    try:
        gs.load_config()
        raise AssertionError("string allow accepted")
    except gs.ConfigError:
        pass
    gs.CONFIG.write_text('{"allow": ["O/R"], "policy_reviewed": ["O/R"]}')
    try:
        gs.load_config()
        raise AssertionError("list policy_reviewed accepted")
    except gs.ConfigError:
        pass
    gs.CONFIG.write_text('{"allow": ["O/R"], "policy_reviewed": {"O/R": [" a line "]}, "_note": "x"}')
    assert gs.load_config() == {"allow": {"o/r"}, "policy_reviewed": {"o/r": {"a line"}}}


def test_ignored_build_output_is_not_compared():
    assert gs.is_regen("node_modules/")
    assert gs.is_regen("apps/web/.next/")
    assert gs.is_regen("debug.log")
    assert not gs.is_regen("apps/api/.env.local")
    assert not gs.is_regen("certs/key.pem")
    assert not gs.is_regen(".wrangler/state/v3/d1/db.sqlite")


def g(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def repo_with_worktree(tmp_path):
    main, wt = tmp_path / "main", tmp_path / "wt"
    g(tmp_path, "init", "-q", "-b", "main", str(main))
    g(main, "config", "user.email", "t@example.com")
    g(main, "config", "user.name", "t")
    (main / ".gitignore").write_text("node_modules/\n.env\n")
    (main / "a.txt").write_text("a\n")
    g(main, "add", ".")
    g(main, "commit", "-qm", "init")
    g(main, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    return main, wt


def test_live_state_sees_hidden_edits_and_paused_operations(tmp_path):
    main, wt = repo_with_worktree(tmp_path)
    assert gs.live_state(str(wt))[1]
    g(wt, "config", "status.showUntrackedFiles", "no")
    (wt / "draft.py").write_text("x")
    assert not gs.live_state(str(wt))[1], "untracked file hidden by config"
    (wt / "draft.py").unlink()
    g(wt, "update-index", "--assume-unchanged", "a.txt")
    (wt / "a.txt").write_text("local edit\n")
    assert not gs.live_state(str(wt))[1], "assume-unchanged edit"
    g(wt, "update-index", "--no-assume-unchanged", "a.txt")
    g(wt, "checkout", "--", "a.txt")
    gitdir = subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"], capture_output=True,
                            text=True).stdout.strip()
    (Path(gitdir) / "rebase-merge").mkdir()
    assert not gs.live_state(str(wt))[1], "paused rebase"


def test_ignored_files_must_match_the_main_checkout(tmp_path):
    main, wt = repo_with_worktree(tmp_path)
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "x.js").write_text("x")
    assert gs.ignored_files_safe(str(wt), str(main))
    (wt / ".env").write_text("KEY=1\n")
    assert not gs.ignored_files_safe(str(wt), str(main))
    (main / ".env").write_text("KEY=1\n")
    assert gs.ignored_files_safe(str(wt), str(main))
    (wt / ".env").write_text("KEY=2\n")
    assert not gs.ignored_files_safe(str(wt), str(main))


def test_remove_keeps_a_recovery_ref_and_a_branch_used_elsewhere(tmp_path):
    main, wt = repo_with_worktree(tmp_path)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    gs.process_cwds = lambda: set()
    w = {"path": str(wt), "head": head, "branch": "feat/x"}
    ok, note = gs.remove_worktree(main, w, "merged into origin/main")
    assert ok and note == "; branch deleted" and not wt.exists()
    refs = subprocess.run(["git", "-C", str(main), "for-each-ref", "refs/zs-git-sweep/"], capture_output=True,
                          text=True).stdout
    assert head in refs
    g(main, "worktree", "add", "-q", "-b", "feat/y", str(wt))
    g(main, "worktree", "add", "-q", "-f", str(tmp_path / "wt2"), "feat/y")
    ok, note = gs.remove_worktree(main, dict(w, branch="feat/y"), "merged into origin/main")
    assert ok and "checked out elsewhere" in note
    (tmp_path / "wt2" / "new.txt").write_text("x")
    ok, note = gs.remove_worktree(main, {"path": str(tmp_path / "wt2"), "head": head, "branch": "feat/y"}, "m")
    assert not ok and (tmp_path / "wt2" / "new.txt").exists()
