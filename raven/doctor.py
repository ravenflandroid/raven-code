from __future__ import annotations

import subprocess
from pathlib import Path

from raven.config import RavenConfig


def run_doctor(config_path: str | Path = "config.yaml") -> int:
    cfg = RavenConfig.load(config_path)
    checks = [
        ("adb", [cfg.android.adb_path, "version"]),
        ("java", ["java", "-version"]),
        ("ffmpeg", [cfg.tools.ffmpeg_path, "-version"]),
        ("joern-parse", [cfg.tools.joern_parse, "--help"]),
        ("joern-export", [cfg.tools.joern_export, "--help"]),
    ]
    failed = 0
    for name, cmd in checks:
        ok, detail = _check_command(cmd)
        print(f"{'OK' if ok else 'FAIL'} {name}: {detail}")
        failed += 0 if ok else 1

    endpoint_ok = cfg.azure_openai.endpoint.startswith("https://") and "YOUR-" not in cfg.azure_openai.endpoint
    key_ok = bool(cfg.azure_openai.api_key) and "YOUR_" not in cfg.azure_openai.api_key
    print(f"{'OK' if endpoint_ok else 'FAIL'} azure endpoint configured")
    print(f"{'OK' if key_ok else 'FAIL'} azure api key configured")
    failed += 0 if endpoint_ok else 1
    failed += 0 if key_ok else 1
    return 1 if failed else 0


def _check_command(cmd: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except FileNotFoundError:
        return False, f"not found: {cmd[0]}"
    except Exception as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").splitlines()
    detail = output[0] if output else f"exit code {result.returncode}"
    return result.returncode == 0 or bool(output), detail[:180]
