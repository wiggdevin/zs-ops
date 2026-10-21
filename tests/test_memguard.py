"""memguard: runner detection, process trees, the stop policy, config, the enabled main path with a
stubbed sampler, and a real SIGTERM/SIGKILL. Portable: no launchctl, no memory pressure, Python 3.9+."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memguard as mg  # noqa: E402

GB = mg.GB
for k in [k for k in os.environ if k.startswith("ZS_MEMGUARD")]:
    del os.environ[k]
CFG = dict(mg.config(), proc_gb=32.0, compressed_gb=16.0, keep_gb=16.0, grace_s=1.0, dry_run=False)
PY = "/repo/.venv/bin/python -m pytest -q tests/test_photo_assign.py"


def tree(root, gb, cmd=PY, pids=None):
    return (root, int(gb * GB), cmd, pids or [root])


def roots(victims):
    return [(v[0], v[4]) for v in victims]


def test_runner_detection():
    for cmd in (PY, "/x/.venv/bin/pytest tests", "/x/.venv/bin/python3.12 /x/.venv/bin/pytest -q",
                "python3 -mpytest", "python -W ignore -X dev -m pytest -x", "python3 -u -m py.test",
                "node /x/node_modules/.bin/vitest run", "node --max-old-space-size=8192 /x/node_modules/jest/bin/jest.js",
                "bun /x/node_modules/vitest/vitest.mjs run", "vitest run", "jest --ci"):
        assert mg.is_runner(cmd), cmd
    for cmd in ("claude -p fix pytest OOM in the vision suite", "codex exec run vitest and jest",
                "python report.py --suite pytest", "python3 -c import pytest; pytest.main()",
                "python3 memo_reader/pytest_helpers.py", "/Applications/T3 Code (Beta).app/Contents/MacOS/T3 Code",
                "llama-server -m model.gguf", "uv run pytest -q", "node server.js --dir /tmp/jest",
                "node /x/node_modules/jest-worker/build/workers/processChild.js", "/bin/zsh -c pytest -q", ""):
        assert not mg.is_runner(cmd), cmd


def test_parse_ps():
    out = "  1     0 /sbin/launchd\n 12     1 /usr/bin/python3 -m pytest -q\n 13    12\n bad line\n"
    assert mg.parse_ps(out) == {1: (0, "/sbin/launchd"), 12: (1, "/usr/bin/python3 -m pytest -q"), 13: (12, "")}


def test_trees_roll_up_workers_and_skip_nested_and_own_ancestors():
    procs = {
        10: (1, "uv run pytest -q"), 11: (10, PY),                      # uv wrapper -> pytest root 11
        12: (11, "python -c import sys;exec(eval(sys.stdin.readline()))"),  # xdist worker
        13: (12, "python -m pytest tests/inner.py"),                    # nested runner: part of 11's tree
        20: (1, "node /x/node_modules/.bin/vitest run"), 21: (20, "node /x/tinypool/dist/process.js"),
        30: (1, PY), 31: (30, "/usr/bin/python3 memguard.py"),         # memguard runs under a pytest
        40: (1, "claude -p pytest"),
    }
    got = dict(mg.runner_trees(procs, me=31))
    assert sorted(got) == [11, 20], got
    assert sorted(got[11]) == [11, 12, 13] and sorted(got[20]) == [20, 21]


def test_calm_machine_stops_nothing():
    assert mg.decide([tree(1, 14), tree(2, 10)], 1 * GB, 1, CFG) == ([], False)


def test_one_tree_past_the_cap_is_stopped_without_pressure():
    victims, pressure = mg.decide([tree(1, 33), tree(2, 12)], 1 * GB, 1, CFG)
    assert roots(victims) == [(1, "proc_cap")] and not pressure


def test_pressure_stops_largest_first_and_keeps_one_run():
    # 2026-10-21 shape: five vision runs, compressor at 25 GB.
    trees = [tree(1, 12), tree(2, 30), tree(3, 20), tree(4, 15), tree(5, 13)]
    victims, pressure = mg.decide(trees, 25 * GB, 1, CFG)
    assert pressure and roots(victims) == [(2, "pressure"), (3, "pressure"), (4, "pressure"), (5, "pressure")]


def test_pressure_keeps_the_last_run_even_above_keep_unless_critical():
    trees = [tree(1, 20), tree(2, 18)]
    assert roots(mg.decide(trees, 25 * GB, 1, CFG)[0]) == [(1, "pressure")]
    assert roots(mg.decide(trees, 25 * GB, 4, CFG)[0]) == [(1, "pressure"), (2, "pressure")]
    assert mg.decide([tree(1, 15)], 1 * GB, 4, CFG) == ([], True)   # within KEEP_GB: nothing to stop


def test_failed_sensors_still_enforce_the_cap():
    victims, pressure = mg.decide([tree(1, 40), tree(2, 20), tree(3, 20)], None, None, CFG)
    assert roots(victims) == [(1, "proc_cap")] and not pressure


def test_config_rejects_bad_numbers():
    env = {"ZS_MEMGUARD_KEEP_GB": "nan", "ZS_MEMGUARD_PROC_GB": "-1", "ZS_MEMGUARD_GRACE_S": "x",
           "ZS_MEMGUARD_COMPRESSED_GB": "inf"}
    os.environ.update(env)
    try:
        cfg = mg.config()
    finally:
        for k in env:
            del os.environ[k]
    assert (cfg["keep_gb"], cfg["proc_gb"], cfg["grace_s"], cfg["compressed_gb"]) == (16.0, 32.0, 5.0, 16.0)
    assert len(cfg["errors"]) == 4


def test_stop_all_terminates_kills_and_never_signals_a_changed_pid():
    soft = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    hard = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                             " print(1, flush=True); time.sleep(30)"], stdout=subprocess.PIPE)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        hard.stdout.readline()
        time.sleep(0.2)
        targets = {soft.pid: mg.identity(soft.pid), hard.pid: mg.identity(hard.pid), other.pid: "not-its-identity"}
        t = time.monotonic()
        got = mg.stop_all(targets, 1.0)
        assert got[hard.pid] == "killed" and time.monotonic() - t >= 1.0, got
        assert got[soft.pid] == "terminated" and got[other.pid] == "gone", got
        assert other.poll() is None                       # a pid that is not the sampled process is left alone
    finally:
        for p in (soft, hard, other):
            p.kill()
            p.wait(5)
    assert mg.identity(soft.pid) is None


def _run_main(fake_procs, compressed, level, env=None):
    """main() with a stubbed sampler and footprint; stop_all records instead of signalling."""
    calls = []
    saved = (mg.sample, mg.footprint, mg.stop_all, mg.identity, mg.notify, mg.LOG, mg.LOCK)
    tmp = Path(tempfile.mkdtemp())
    mg.sample = lambda: (fake_procs, compressed, level)
    mg.footprint = lambda pid: {11: 20 * GB, 12: 5 * GB, 20: 18 * GB, 30: 40 * GB}.get(pid, 0)
    mg.stop_all = lambda targets, grace: calls.append(sorted(targets)) or {p: "terminated" for p in targets}
    mg.identity = lambda pid: "id%d" % pid
    mg.notify = lambda msg: None
    mg.LOG, mg.LOCK = tmp / "log.jsonl", tmp / "lock"
    os.environ.update(env or {})
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mg.main()
        log = [json.loads(x) for x in mg.LOG.read_text().splitlines()] if mg.LOG.exists() else []
        return code, json.loads(buf.getvalue()), calls, log
    finally:
        mg.sample, mg.footprint, mg.stop_all, mg.identity, mg.notify, mg.LOG, mg.LOCK = saved
        for k in env or {}:
            del os.environ[k]


PROCS = {11: (1, PY), 12: (11, "python worker"), 20: (1, "node /x/node_modules/.bin/vitest run"),
         40: (1, "llama-server -m m.gguf")}


def test_main_stops_whole_trees_and_logs_before_and_after():
    code, out, calls, log = _run_main(PROCS, 25 * GB, 1)
    assert code == 0 and out["status"] == "stopped", out
    assert calls == [[11, 12]]                                       # pytest tree (25 GB); vitest (18 GB) kept
    assert [r["phase"] for r in log] == ["stopping", "done"]
    assert log[1]["stopped"][0]["result"] == {"11": "terminated", "12": "terminated"}
    assert '"' not in out["summary"] and "pid 11" in out["summary"], out["summary"]


def test_main_dry_run_and_degraded_and_ok():
    code, out, calls, log = _run_main(PROCS, 25 * GB, 1, {"ZS_MEMGUARD_DRY_RUN": "1"})
    assert out["status"] == "dry_run" and calls == [] and [r["phase"] for r in log] == ["dry_run"]
    code, out, calls, _ = _run_main(PROCS, None, None)
    assert out["status"] == "degraded" and calls == [] and len(out["errors"]) == 2
    code, out, calls, _ = _run_main(PROCS, 1 * GB, 1)
    assert out["status"] == "ok" and calls == [] and out["stopped"] == []


def test_main_reports_a_failed_process_list():
    saved = mg.sample

    def broken():
        raise mg.SampleError("ps exit 1, 0 processes")
    mg.sample = broken
    try:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mg.main()
    finally:
        mg.sample = saved
    assert code == 1 and json.loads(buf.getvalue())["status"] == "error"


def test_log_rotation_keeps_the_newer_half():
    saved, tmp = (mg.LOG, mg.LOG_CAP_BYTES), Path(tempfile.mkdtemp())
    mg.LOG, mg.LOG_CAP_BYTES = tmp / "log.jsonl", 60
    try:
        for i in range(20):
            mg.append_log({"i": i})
        rows = [json.loads(x)["i"] for x in mg.LOG.read_text().splitlines()]
        assert rows[-1] == 19 and len(rows) < 20 and not (tmp / "log.jsonl.tmp").exists()
    finally:
        mg.LOG, mg.LOG_CAP_BYTES = saved


def test_off_switch():
    out = subprocess.run([sys.executable, str(Path(mg.__file__))], env=dict(os.environ, ZS_MEMGUARD="0"),
                         capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)["status"] == "off"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
