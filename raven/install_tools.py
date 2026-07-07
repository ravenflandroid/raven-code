from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


JOERN_URL = "https://github.com/joernio/joern/releases/latest/download/joern-cli.zip"


def install_joern(target: str | Path = "C:/Tools/joern") -> int:
    target_path = Path(target)
    zip_path = target_path.parent / "joern-cli.zip"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Invoke-WebRequest -Uri '{JOERN_URL}' -OutFile '{zip_path}'",
        ],
        check=True,
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Expand-Archive -Force '{zip_path}' '{target_path}'",
        ],
        check=True,
    )
    print(f"Joern extracted to {target_path}")
    print("Find scripts with:")
    print(f"  Get-ChildItem {target_path} -Recurse -Filter 'joern-parse*'")
    print(f"  Get-ChildItem {target_path} -Recurse -Filter 'joern-export*'")
    print("Then set tools.joern_parse and tools.joern_export in config.yaml to the .bat paths.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install optional RAVEN external tools")
    sub = parser.add_subparsers(dest="command", required=True)
    joern = sub.add_parser("joern", help="Download and extract Joern CLI")
    joern.add_argument("--target", default="C:/Tools/joern")
    args = parser.parse_args(argv)
    if args.command == "joern":
        return install_joern(args.target)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
