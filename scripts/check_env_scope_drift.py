#!/usr/bin/env python3
"""Compatibility wrapper for the v1 env scope checker."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.env_scope_checker import main


if __name__ == "__main__":
    argv = sys.argv[1:] or ["--vps", "all", "--report", "json"]
    raise SystemExit(main(argv))
