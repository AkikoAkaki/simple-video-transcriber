"""Queue files from the Explorer "转录" menu by handing them to the tray, starting it when needed.

Explorer runs ``pythonw enqueue.py <file>`` once per selected file; setup_context_menu.ps1 registers it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import api
import config

TRAY_START_TIMEOUT_SECONDS = 60
# A system proxy must not see requests to the tray; Windows proxy bypass rules skip only dotless hosts.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(paths: list[str]) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.API_PORT}/api/jobs", data=json.dumps({"paths": paths}).encode("utf-8"),
        headers={"Authorization": f"Bearer {api.load_secret()}", "Content-Type": "application/json"},
        method="POST")
    with _OPENER.open(request, timeout=10) as response:
        return json.loads(response.read())


def _start_tray() -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, str(Path(__file__).with_name("tray_app.py")), "--background"],
                     creationflags=flags, close_fds=True)


def _alert(message: str) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Meeting Transcriber", 0x30)
    else:
        print(message, file=sys.stderr)


def main(argv: list[str]) -> int:
    paths = [str(Path(arg).resolve()) for arg in argv]
    if not paths:
        return 0
    started = False
    deadline = time.monotonic() + TRAY_START_TIMEOUT_SECONDS
    while True:
        try:
            _post(paths)
            return 0
        except urllib.error.HTTPError as exc:
            # A tray that is still starting may not have written its secret yet.
            if exc.code != 401:
                _alert(f"Could not queue {Path(paths[0]).name}: {exc.read().decode('utf-8', 'replace')}")
                return 1
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if not started:
                _start_tray()
                started = True
        if time.monotonic() > deadline:
            _alert("The transcriber tray did not start within a minute. Run start.bat and try again.")
            return 1
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
