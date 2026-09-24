#!/usr/bin/env python3
"""Runner for the full ROMForge Extreme Edge-Case Test Harness (Engines A through J)."""
import os
import sys
import time
import unittest
from pathlib import Path

# Add workspace root to sys.path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))


def main():
    print("=" * 70)
    print("  ROMForge 10-Engine Extreme Edge-Case Harness (A through J)")
    print("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(root / "harness"), pattern="test_*.py")

    start_time = time.time()
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = time.time() - start_time

    print("-" * 70)
    print(f"Harness executed in {elapsed:.2f} seconds.")
    print(f"Tests run: {result.testsRun} | Failures: {len(result.failures)} | Errors: {len(result.errors)}")

    if result.wasSuccessful():
        print("\033[92mALL 10 HARNESS ENGINES PASSED GREEN!\033[0m")
        return 0
    else:
        print("\033[91mHARNESS FAILED — FIX REGRESSIONS BEFORE COMMITTING\033[0m")
        return 1


if __name__ == "__main__":
    sys.exit(main())
