"""machine_map.latest_dream_digests — nested Inbox layout + legacy flat files."""
import importlib.util, os, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "mm", str(Path(__file__).resolve().parents[1] / "machine_map.py"))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "dream-digest-2026-10-12.md").write_text("old")
    nested = root / "mac-ops" / "2026-10"
    nested.mkdir(parents=True)
    (nested / "2026-10-13-mac-ops-dream-digest.md").write_text("new")
    (nested / "2026-10-13-mac-ops-report-cleanup.md").write_text("other")
    arch = root / "_archive" / "2026-10"
    arch.mkdir(parents=True)
    (arch / "dream-digest-2026-12-31.md").write_text("archived")
    got = [os.path.relpath(p, td) for p in mm.latest_dream_digests(td)]
    assert got == ["dream-digest-2026-10-12.md", "mac-ops/2026-10/2026-10-13-mac-ops-dream-digest.md"], got
    assert mm.latest_dream_digests(str(root / "missing")) == []
print("PASS machine_map latest_dream_digests")
