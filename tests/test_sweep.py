"""sweep: time parsing, protection, and the auto / review / watch rules on stubbed process tables.
Portable: no real processes are signalled. Python 3.9+."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sweep as sw  # noqa: E402

H = 3600
GB = sw.mg.GB
sw.mg.facts = lambda pid: (FOOT.get(pid, 0), 1)
sw.mg.identity = lambda pid: "id%d" % pid
sw.bound_identity = lambda pid, cmd: "id%d" % pid
FOOT = {}


def proc(ppid, age, cpu_s, cmd, tty="??"):
    return {"ppid": ppid, "age": age, "cpu_s": cpu_s, "tty": tty, "cmd": cmd}


def run(table, cpu_delta, jobs=frozenset()):
    """table {pid: proc}; cpu_delta {pid: cpu seconds used in the window}."""
    after = {p: dict(v, cpu_s=v["cpu_s"] + cpu_delta.get(p, 0)) for p, v in table.items()}
    auto, review, watch = sw.classify(table, after, set(jobs), set())
    return {i["pid"]: i.get("rule") for i in auto}, {i["pid"] for i in review}, {i["pid"]: i["protected"] for i in watch}


def test_time_parsing():
    assert sw.to_seconds("01:02") == 62
    assert sw.to_seconds("1-02:00:00") == 86400 + 7200
    assert abs(sw.to_seconds("3:01.50") - 181.5) < 1e-9


def test_auto_rules():
    """Nothing is stopped unasked: runaway, stale and orphaned-shell candidates all go to review
    (a nohup job also has parent pid 1)."""
    w = sw.WINDOW_S
    table = {
        10: proc(1, 2 * H, 0, "node /x/server.js"),                       # runaway orphan -> review
        11: proc(1, 7 * H, 0, "python3 train.py"),                        # stale orphan -> review
        13: proc(1, 3 * H, 0, "/bin/zsh -c source /u/.claude/shell-snapshots/s.sh && x"),  # orphaned shell
        14: proc(1, 10 * 60, 0, "node /x/young.js"),                      # young: ignored
        15: proc(1, 30 * 60, 0, "/bin/zsh -c source /u/.claude/shell-snapshots/t.sh && y"),  # young shell
    }
    auto, review, watch = run(table, {10: w * 0.95})
    assert auto == {}
    assert review == {10, 11, 13}
    assert watch == {}


def test_protected_descendant_protects_the_tree():
    """An orphaned wrapper whose child is a Claude session or has a terminal is never stopped."""
    table = {
        50: proc(1, 3 * H, 0, "/bin/zsh -c source /u/.claude/shell-snapshots/s.sh && claude"),
        51: proc(50, 3 * H, 0, "claude --resume"),
        60: proc(1, 3 * H, 0, "node /x/wrapper.js"),
        61: proc(60, 3 * H, 0, "node /x/repl.js", tty="ttys004"),
    }
    auto, review, watch = run(table, {})
    assert auto == {} and review == set()
    assert "below it" in sw.protected_reason(table, 60, set(), set())
    assert sw.stop_tree(table, 60, "id60", set(), set()).startswith("refused")


def test_unreadable_identity_is_refused():
    table = {70: proc(1, 3 * H, 0, "node /x/a.js")}
    real = sw.mg.identity
    sw.mg.identity = lambda pid: None
    try:
        assert sw.stop_tree(table, 70, None, set(), set()).startswith("refused")
        assert sw.stop_tree(table, 70, "None", set(), set()).startswith("refused")
    finally:
        sw.mg.identity = real


def test_launchd_listing_failure_is_unknown():
    class R:
        returncode, stdout = 1, ""
    real = sw.subprocess.run
    sw.subprocess.run = lambda *a, **k: R()
    try:
        assert sw.launchd_pids() is None
    finally:
        sw.subprocess.run = real


def test_protection():
    w = sw.WINDOW_S
    table = {
        20: proc(1, 5 * H, 0, "node /x/svc.js"),                                    # launchd job
        21: proc(20, 5 * H, 0, "node /x/child.js"),                                  # below launchd job
        22: proc(1, 5 * H, 0, "/Applications/ego lite.app/Contents/MacOS/ego lite"), # app
        23: proc(1, 5 * H, 0, "claude --resume"),                                    # agent tool
        24: proc(23, 5 * H, 0, "node /x/vite.js"),                                   # below agent
        25: proc(1, 5 * H, 0, "node /x/a.js", tty="ttys003"),                        # has terminal
        26: proc(1, 5 * H, 0, "/Users/u/.local/bin/python3 -m hermes_cli"),          # hermes via python
    }
    FOOT.clear()
    FOOT[22] = int(6 * GB)
    auto, review, watch = run(table, {p: w for p in table}, jobs={20})
    assert auto == {} and review == set()
    assert set(watch) == set(table)  # all hot, all protected
    assert watch[20] == "launchd job" and watch[21].startswith("below launchd job")
    assert watch[22] == "app or system binary" and watch[23] == "agent tool"
    assert watch[24].startswith("below agent tool") and watch[25] == "has terminal"


def test_unreadable_launchd_list_protects_orphans():
    table = {30: proc(1, 8 * H, 0, "node /x/a.js")}
    auto, review, watch = sw.classify(table, table, None, set())
    assert auto == [] and review == []


def test_hot_process_with_live_parent_is_review_only():
    table = {40: proc(39, 2 * H, 0, "node /x/loop.js"), 39: proc(1, 2 * H, 0, "make")}
    auto, review, watch = run(table, {40: sw.WINDOW_S})
    assert auto == {} and review == {40}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
