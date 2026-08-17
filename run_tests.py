"""Run the offline test suites.

    python run_tests.py

There is no pytest here and no plan to add one: the suites are plain scripts
that assert at module level and exit non-zero on failure, so `python -m pytest`
finds nothing to collect. This script is the one command that runs them all.

Each suite is run as a SEPARATE PROCESS on purpose. Every one of them points
EARNINGS_DB_PATH (and the upload/report folders) at a throwaway workspace
*before* importing app, so importing two of them into one interpreter would
give the second whatever the first had already configured — and a suite that
silently ran against the reviewer's real pdfs.db is exactly the accident the
review gate exists to prevent.

Suites are discovered by glob rather than listed, so a new test_*.py file is
picked up without anyone remembering to edit this script. Nothing here makes a
network call; the suites assert that themselves.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _assertion_count(output: str) -> int:
    """How many check() assertions the suite actually ran.

    Counted from the output rather than reported by the suites, because a
    number a suite prints about itself can drift from what it does."""
    return sum(1 for line in output.splitlines()
               if line.startswith("  PASS  ") or line.startswith("  FAIL  "))


def main() -> int:
    suites = sorted(os.path.basename(p)
                    for p in glob.glob(os.path.join(BASE_DIR, "test_*.py")))
    if not suites:
        print("No test_*.py files found — nothing was verified.")
        return 1

    failed = []
    total_assertions = 0

    for name in suites:
        started = time.time()
        proc = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, name)],
            cwd=BASE_DIR, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        ran = _assertion_count(output)
        total_assertions += ran
        elapsed = time.time() - started

        if proc.returncode == 0:
            print(f"PASS  {name:<28} {ran:>4} checks  {elapsed:5.1f}s")
        else:
            failed.append(name)
            print(f"FAIL  {name:<28} {ran:>4} checks  {elapsed:5.1f}s")
            # A runner that hides which check failed is no better than no
            # runner, so the failing suite's own output is reproduced whole.
            print(f"--- {name} output " + "-" * (58 - len(name)))
            print(output.rstrip())
            print("-" * 76)

    print()
    print(f"{len(suites) - len(failed)}/{len(suites)} suites passed, "
          f"{total_assertions} checks run.")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
