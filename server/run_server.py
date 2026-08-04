#!/usr/bin/env python3
"""Entry point for the bundled MCP server.

Deliberately dependency-free and launched by path, so `.mcp.json` can point a
bare `python3` at it with no install step, virtualenv, or PYTHONPATH juggling.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeact_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
