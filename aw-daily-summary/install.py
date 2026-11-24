"""Install or remove the logon launcher for daily_summary.py (Windows)."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent / "daily_summary.py"
LAUNCHER_NAME = "aw-daily-summary.vbs"


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot locate the Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_path():
    return startup_dir() / LAUNCHER_NAME


def pythonw_path():
    candidates = [
        Path(sys.executable).resolve(),
        Path(sys.executable).resolve().with_name("pythonw.exe"),
        Path(sys.prefix) / "pythonw.exe",
    ]
    for candidate in candidates:
        if candidate.name.lower() == "pythonw.exe" and candidate.is_file():
            return candidate
    raise RuntimeError(
        "pythonw.exe not found next to " + sys.executable + "; run install.py with python.exe"
    )


def vbs_quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def build_launcher():
    interpreter = pythonw_path()
    command = vbs_quote(f"{vbs_quote(interpreter)} {vbs_quote(SCRIPT_PATH)}")
    return (
        "Set WshShell = CreateObject(\"WScript.Shell\")\n"
        f"WshShell.Run {command}, 0, False\n"
    )


def install():
    if not SCRIPT_PATH.is_file():
        raise RuntimeError(f"missing script: {SCRIPT_PATH}")
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_launcher(), encoding="utf-8")
    print(f"Installed: {path}")
    print(f"Runs:      {SCRIPT_PATH}")
    print(f"Using:     {pythonw_path()}")


def uninstall():
    path = launcher_path()
    if path.is_file():
        path.unlink()
        print(f"Removed: {path}")
    else:
        print(f"Not installed: {path}")


def status():
    path = launcher_path()
    print(f"Launcher: {path}")
    print(f"Present:  {path.is_file()}")
    print(f"Script:   {SCRIPT_PATH}")
    if path.is_file():
        print("--- content ---")
        print(path.read_text(encoding="utf-8"), end="")


def test():
    return subprocess.call([sys.executable, str(SCRIPT_PATH), "--force", "--print"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="install the startup launcher (default)")
    group.add_argument("--uninstall", action="store_true", help="remove the startup launcher")
    group.add_argument("--status", action="store_true", help="show launcher status")
    group.add_argument("--test", action="store_true", help="print a preview recap")
    args = parser.parse_args(argv)

    try:
        if args.uninstall:
            uninstall()
        elif args.status:
            status()
        elif args.test:
            return test()
        else:
            install()
    except (OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
