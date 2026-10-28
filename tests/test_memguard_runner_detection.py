"""Regression fixtures for recognizing executable test runners without signals."""
import importlib.util
from pathlib import Path
import unittest
SPEC = importlib.util.spec_from_file_location("memguard", Path(__file__).resolve().parents[1] / "memguard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

class RunnerDetectionTests(unittest.TestCase):
    def test_non_runners_remain_untouched(self):
        cases = ["node --eval=setInterval(()=>{},1000) jest", "node --print=process.pid vitest",
                 "node -efoo jest", "node -pfoo vitest", "node --diagnostic-dir /tmp/jest /app/server.js",
                 "node --icu-data-dir /tmp/vitest /app/server.js", "node --future-option /tmp/jest /app/server.js",
                 "codex exec --model gpt-6.1-sol pytest", "node server.js jest"]
        for command in cases:
            with self.subTest(command=command): self.assertFalse(guard.is_runner(command))
    def test_known_runners(self):
        cases = ["node --max-old-space-size=4096 /tmp/vitest/vitest.mjs", "node --require setup.js /app/jest.js",
                 "node --experimental-vm-modules /app/jest.js", "node -- /app/jest.js", "python3 -m pytest -n 4", "bun test"]
        for command in cases:
            with self.subTest(command=command): self.assertTrue(guard.is_runner(command))
if __name__ == "__main__": unittest.main()
