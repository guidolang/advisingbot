#!/usr/bin/env python3
"""Start the bundled AdvisingBot app and open it in the default browser."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app" / "app.py"


def responding(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except OSError:
        return False


def opentabs_problem() -> tuple[str | None, bool]:
    """Check OpenTabs and return whether this launcher started it."""
    command = shutil.which("opentabs")
    if not command:
        return (
            "AdvisingBot needs its secure Stellic connection, but it is not "
            "available on this computer. Ask Codex to set up OpenTabs for "
            "AdvisingBot. You do not need to install or configure anything yourself.",
            False,
        )

    def status() -> dict | None:
        try:
            result = subprocess.run(
                [command, "status", "--json"], capture_output=True,
                text=True, timeout=8, check=False,
            )
            return json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    current = status()
    started_by_launcher = False
    if current is None:
        try:
            started = subprocess.run(
                [command, "start", "--background"], capture_output=True,
                text=True, timeout=12, check=False,
            )
            started_by_launcher = started.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        for _ in range(12):
            current = status()
            if current is not None:
                break
            time.sleep(0.5)

    if current is None or current.get("status") != "ok":
        return (
            "AdvisingBot could not start its secure Stellic connection. "
            "Ask Codex to repair OpenTabs for AdvisingBot, then try again.",
            False,
        )
    if not current.get("extensionConnected"):
        return (
            "AdvisingBot's secure browser connection is ready but not connected "
            "to Chrome. Ask Codex to reconnect OpenTabs to Chrome, then try again.",
            False,
        )
    return None, started_by_launcher


def stop_opentabs(command: str | None, started_by_launcher: bool) -> None:
    """Stop only the OpenTabs server this launcher started."""
    if not started_by_launcher or not command:
        return
    try:
        result = subprocess.run(
            [command, "stop"], capture_output=True, text=True,
            timeout=12, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(
            "AdvisingBot closed, but its secure Stellic connection could not be "
            "stopped. Ask Codex to close OpenTabs.",
            file=sys.stderr,
        )
        return
    if result.returncode != 0:
        print(
            "AdvisingBot closed, but its secure Stellic connection could not be "
            "stopped. Ask Codex to close OpenTabs.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Start AdvisingBot locally.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"

    # A previous call may have left the local app running after its browser
    # window closed. Reuse it without touching its OpenTabs connection.
    if responding(url):
        webbrowser.open(url, new=1)
        print(f"AdvisingBot is already running at {url}")
        return 0

    problem, started_by_launcher = opentabs_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2

    try:
        process = subprocess.Popen([sys.executable, str(APP), "--port", str(args.port)], cwd=APP.parent)
        for _ in range(40):
            if responding(url):
                webbrowser.open(url, new=1)
                print(f"AdvisingBot is running at {url}")
                try:
                    return process.wait()
                except KeyboardInterrupt:
                    process.terminate()
                    return process.wait()
            if process.poll() is not None:
                return process.returncode or 1
            time.sleep(0.25)

        process.terminate()
        process.wait()
        print("AdvisingBot did not start. No browser window was opened.", file=sys.stderr)
        return 1
    finally:
        stop_opentabs(shutil.which("opentabs"), started_by_launcher)


if __name__ == "__main__":
    raise SystemExit(main())
