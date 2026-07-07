from __future__ import annotations

import argparse
from pathlib import Path

from raven.config import RavenConfig
from raven.pipeline import RavenPipeline


class Console:
    def print(self, message: str) -> None:
        print(message.replace("[bold]", "").replace("[/bold]", ""))


try:
    from rich.console import Console as RichConsole

    console = RichConsole()
except ModuleNotFoundError:
    console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raven", description="RAVEN Android bug localization pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the full RAVEN workflow")
    run.add_argument("--config", default="config.yaml", help="Path to RAVEN config.yaml")
    run.add_argument("--repo-url", required=True, help="Android app Git repository URL")
    run.add_argument("--issue-url", required=True, help="GitHub issue URL")
    run.add_argument("--apk", required=False, type=Path, default=None, help="APK path to install on the emulator (omit with --skip-install)")
    run.add_argument("--skip-install", action="store_true", default=False, help="Skip APK installation (use app already on emulator)")
    run.add_argument("--media", type=Path, default=None, help="Optional local bug video or final screenshot")
    run.add_argument("--emulator-serial", default=None, help="ADB serial, for example emulator-3554")
    run.add_argument("--package-name", default=None, help="Android package name")
    run.add_argument("--launch-activity", default=None, help="Fully qualified package/activity for adb am start")

    ui = sub.add_parser("ui", help="Run the local RAVEN web UI")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)

    doctor = sub.add_parser("doctor", help="Check external RAVEN dependencies")
    doctor.add_argument("--config", default="config.yaml", help="Path to RAVEN config.yaml")

    install = sub.add_parser("install-tools", help="Install optional external tools")
    install_sub = install.add_subparsers(dest="tool", required=True)
    joern = install_sub.add_parser("joern", help="Download and extract Joern CLI")
    joern.add_argument("--target", default="C:/Tools/joern")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        config = RavenConfig.load(args.config)
        if args.emulator_serial:
            config.android.emulator_serial = args.emulator_serial
        apk_path = args.apk
        if apk_path is None and not args.skip_install and config.android.apk_path:
            apk_path = Path(config.android.apk_path)
        result = RavenPipeline(config).run(
            repo_url=args.repo_url,
            issue_url=args.issue_url,
            apk_path=apk_path,
            skip_install=args.skip_install,
            media_path=args.media,
            package_name=args.package_name,
            launch_activity=args.launch_activity,
        )
        console.print(f"[bold]RAVEN run complete[/bold]: {result.run_dir}")
        console.print(f"Report: {result.report_path}")
        console.print(f"HDG: {result.hdg_path}")
        console.print(f"Localization: {result.localization_path}")
        return 0
    if args.command == "ui":
        from raven.ui.server import run_server

        run_server(args.host, args.port)
        return 0
    if args.command == "doctor":
        from raven.doctor import run_doctor

        return run_doctor(args.config)
    if args.command == "install-tools":
        if args.tool == "joern":
            from raven.install_tools import install_joern

            return install_joern(args.target)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
