#!/usr/bin/env python3
"""Run the whole suite against a throwaway CODEACT_HOME."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="codeact-tests-") as tmp:
        os.environ["CODEACT_HOME"] = tmp
        suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
