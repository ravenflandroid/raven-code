#!/usr/bin/env python3
"""
RAVEN Reproduction Package — UI Launcher
=========================================
Usage:
    python run_ui.py              # opens http://127.0.0.1:8765
    python run_ui.py --port 9000
    python run_ui.py --open       # auto-open browser
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
VENV_PYTHON = REPO_ROOT / "model" / ".venv" / "Scripts" / "python.exe"
SERVER = Path(__file__).parent / "server.py"


def main() -> int:
    python = VENV_PYTHON if VENV_PYTHON.exists() else sys.executable
    cmd = [str(python), str(SERVER)] + sys.argv[1:]
    print(f"Python  : {python}")
    print(f"Server  : {SERVER}")
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
