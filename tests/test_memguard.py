"""memguard: the stop policy, the parsers, and a real SIGTERM/SIGKILL. Portable (no launchctl, no pressure)."""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memguard as mg  # noqa: E402

GB = 1024 ** 3
os.environ.pop("ZS_MEMGUARD_KILLABLE", None)
CFG = dict(mg.config(), proc_gb=32.0, compressed_gb=16.0, keep_gb=16.0, grace_s=1.0, dry_run=False)
PY = "/repo/.venv/bin/python -m pytest -q tests/test_photo_assign.py"


def pids(victims):
    return [(v[0], v[3]) for v in victims]


def test_parsers():
    assert mg.parse_mem("10G") == 10 * GB and mg.parse_mem("823M") == 823 * 1024 ** 2
    assert mg.parse_mem("512K+") == 512 * 1024 and mg.parse_mem("1.5G-") == int(1.5 * GB)
    assert mg.parse_mem("n/a") is None
    top = "Processes: 700 total\nPID    MEM  COMMAND\n12634  10G  python3.12\n404    755M WindowServer\n"
    assert mg.footprints(top) == {12634: 10 * GB, 404: 755 * 1024 ** 2}
    assert mg.commands("  12 /usr/bin/python3 -m pytest -q\n 13 /Applications/T3 Code.app/x --y\n") == {
        12: "/usr/bin/python3 -m pytest -q", 13: "/Applications/T3 Code.app/x --y"}
    vm = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages occupied by compressor:   1000.\n"
    assert mg.compressed_bytes(vm) == 1000 * 16384


def test_killable_pattern_matches_test_runners_only():
    import re
    pat = re.compile(CFG["killable"])
    for cmd in (PY, "/x/.venv/bin/pytest tests", "uv run pytest -q", "node /x/node_modules/.bin/vitest run",
                "node /x/node_modules/jest/bin/jest.js --ci", "python3 -m pytest"):
        assert pat.search(cmd), cmd
    for cmd in ("/Applications/T3 Code (Beta).app/Contents/MacOS/T3 Code", "llama-server -m model.gguf",
                "/tmp/dprobe.py foundrae", "python3 memo_reader/pytest_helpers.py", "claude --output-format stream-json"):
        assert not pat.search(cmd), cmd


def test_calm_machine_stops_nothing():
    procs = [(1, 14 * GB, PY), (2, 30 * GB, "llama-server")]
    assert mg.decide(procs, 1 * GB, 1, CFG) == ([], "")


def test_one_runner_past_the_cap_is_stopped_without_pressure():
    procs = [(1, 33 * GB, PY), (2, 12 * GB, PY)]
    victims, reason = mg.decide(procs, 1 * GB, 1, CFG)
    assert pids(victims) == [(1, "proc_cap")] and reason == "proc_cap"


def test_pressure_stops_runners_largest_first_and_keeps_one_run():
    # 2026-10-21 shape: five vision runs, compressor at 25 GB.
    procs = [(1, 12 * GB, PY), (2, 30 * GB - 1, PY), (3, 20 * GB, PY), (4, 15 * GB, PY), (5, 13 * GB, PY),
             (9, 40 * GB, "llama-server")]
    victims, reason = mg.decide(procs, 25 * GB, 1, CFG)
    assert reason == "pressure"
    assert pids(victims) == [(2, "pressure"), (3, "pressure"), (4, "pressure"), (5, "pressure")]   # 12 GB left
    assert 9 not in [v[0] for v in victims]                 # never anything outside the pattern


def test_critical_level_counts_as_pressure_and_the_guard_never_stops_itself():
    procs = [(1, 20 * GB, PY), (os.getpid(), 20 * GB, PY)]
    victims, _ = mg.decide(procs, 1 * GB, 4, CFG)
    assert pids(victims) == [(1, "pressure")]


def test_stop_terminates_then_kills():
    soft = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    hard = subprocess.Popen([sys.executable, "-c",
                             "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(1, flush=True); time.sleep(30)"],
                            stdout=subprocess.PIPE)
    hard.stdout.readline()
    assert mg.stop(soft.pid, 1.0) in ("terminated", "gone")
    t = time.time()
    assert mg.stop(hard.pid, 0.5) == "killed" and time.time() - t >= 0.5
    for p in (soft, hard):
        p.wait(5)
    assert mg.stop(soft.pid, 0.1) == "gone"


def test_off_switch(capsys=None):
    out = subprocess.run([sys.executable, str(Path(mg.__file__))], env=dict(os.environ, ZS_MEMGUARD="0"),
                         capture_output=True, text=True)
    assert out.returncode == 0 and '"off"' in out.stdout


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
