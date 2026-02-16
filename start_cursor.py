#!/usr/bin/env python3
"""
start_cursor.py
Find and launch Cursor IDE with CDP (Chrome DevTools Protocol) enabled.

This is required for PocketCursor to communicate with Cursor.
Run this instead of launching Cursor normally, or add the flags to your
existing shortcut.

Usage:
    python start_cursor.py              # launch or report status
    python start_cursor.py --check      # just check if CDP is available
    python start_cursor.py --port 9225  # force a specific port

Scenarios handled:
    A. Cursor not installed          → error with platform-specific hint
    B. No Cursor running             → fresh launch with CDP on port 9222
    C. Port 9222 taken (non-Cursor)  → auto-find next available port
    D. Cursor running WITH CDP       → launch new window with free port;
                                       verify merge (target count up) or
                                       separate instance (new CDP endpoint)
    E. Cursor running WITHOUT CDP    → inform user, exit (can't enable CDP
                                       on a running process; can't kill
                                       automatically due to unsaved work)
    F. Fresh launch, bind fails      → kill process, retry next port (×3)
    G. --port explicit, bind fails   → error, exit (user chose the port)
    H. --check, CDP available        → report port, exit 0
    I. --check, no CDP               → report status, exit 1
    J. wmic/ps fails (permissions)   → falls through to fresh launch
    K. New window slow to appear     → soft warning, report CDP port anyway
    L. Cursor crashes after launch   → verify times out, retry loop, error

On Windows, multiple Cursor windows share one process and one CDP port.
New windows merge into the existing process regardless of port flags.
Truly separate instances would require --user-data-dir (edge case).

Testing:
    # Scenario H: check mode, CDP available
    python start_cursor.py --check

    # Scenario D: launch with existing Cursor + CDP running
    python start_cursor.py

    # Scenario D + --port ignored warning
    python start_cursor.py --port 9225

    # Scenario B: fresh launch (close all Cursor windows first)
    python start_cursor.py

    # Scenario E: running without CDP (start Cursor via normal shortcut first)
    python start_cursor.py

    # Scenario I: check mode, no CDP (start Cursor via normal shortcut first)
    python start_cursor.py --check
"""

import re
import socket
import subprocess
import sys
import os
import time
from pathlib import Path

BASE_PORT = 9222


def find_cursor():
    """Auto-detect Cursor executable path."""

    if sys.platform == 'win32':
        # Standard Windows install location
        candidates = [
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'cursor' / 'Cursor.exe',
        ]
        for p in candidates:
            if p.exists():
                return str(p)

    elif sys.platform == 'darwin':
        # macOS
        app = Path('/Applications/Cursor.app/Contents/MacOS/Cursor')
        if app.exists():
            return str(app)

    else:
        # Linux — check if cursor is in PATH
        import shutil
        cursor = shutil.which('cursor')
        if cursor:
            return cursor
        # Common install locations
        for p in [Path('/usr/bin/cursor'), Path('/usr/local/bin/cursor')]:
            if p.exists():
                return str(p)

    return None


def is_cursor_running():
    """Check if any Cursor process is running."""
    if sys.platform == 'win32':
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', "name='Cursor.exe'",
                 'get', 'processid'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=15
            )
            return any(line.strip().isdigit() for line in result.stdout.splitlines())
        except Exception:
            return False
    else:
        try:
            result = subprocess.run(
                ['pgrep', '-f', 'Cursor'], capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False


def get_used_ports():
    """Find CDP ports already in use by running Cursor instances."""
    used = set()

    if sys.platform == 'win32':
        try:
            result = subprocess.run(
                ['wmic', 'process', 'where', "name='Cursor.exe'",
                 'get', 'commandline'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=15
            )
            for match in re.findall(r'--remote-debugging-port=(\d+)', result.stdout):
                used.add(int(match))
        except Exception:
            pass
    else:
        # Linux / macOS
        try:
            result = subprocess.run(
                ['ps', 'aux'], capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if 'Cursor' in line or 'cursor' in line:
                    for match in re.findall(r'--remote-debugging-port=(\d+)', line):
                        used.add(int(match))
        except Exception:
            pass

    return sorted(used)


def port_is_open(port):
    """Check if a port is already listening (bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('localhost', port)) == 0


def find_available_port(exclude=None, quiet=False):
    """Find the next available CDP port starting from BASE_PORT."""
    used = get_used_ports()
    if exclude:
        used = sorted(set(used) | exclude)
    if used and not quiet:
        print(f"Ports in use or excluded: {used}")

    port = BASE_PORT
    while True:
        if port not in used and not port_is_open(port):
            return port
        port += 1


def count_page_targets(port):
    """Count Cursor page targets on a CDP port."""
    import urllib.request
    import json as _json
    try:
        with urllib.request.urlopen(f'http://localhost:{port}/json', timeout=3) as resp:
            targets = _json.loads(resp.read())
            return sum(1 for t in targets if t.get('type') == 'page'
                       and 'Cursor' in t.get('title', ''))
    except Exception:
        return 0


def verify_cdp(port, timeout=15):
    """Poll the CDP endpoint until it responds or timeout is reached.
    
    With timeout=0, performs a single instant check (no polling).
    """
    import urllib.request
    import urllib.error

    url = f'http://localhost:{port}/json'
    deadline = time.time() + timeout

    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def main():
    cursor_path = find_cursor()

    if not cursor_path:
        print("ERROR: Could not find Cursor IDE installation.")
        print()
        if sys.platform == 'win32':
            print("Expected location: %LOCALAPPDATA%\\Programs\\cursor\\Cursor.exe")
        elif sys.platform == 'darwin':
            print("Expected location: /Applications/Cursor.app")
        else:
            print("Expected: 'cursor' in PATH or /usr/bin/cursor")
        print()
        print("If Cursor is installed elsewhere, launch it manually with:")
        print("  cursor --remote-debugging-port=9222 --remote-allow-origins=http://localhost:9222")
        sys.exit(1)

    # --check mode: just report status, don't launch anything
    if '--check' in sys.argv:
        ports = get_used_ports()
        if ports:
            print(f"Cursor is running with CDP on port {ports[0]}.")
            targets = count_page_targets(ports[0])
            print(f"Windows: {targets}")
            sys.exit(0)
        elif is_cursor_running():
            print("Cursor is running but without CDP.")
            sys.exit(1)
        else:
            print("Cursor is not running.")
            sys.exit(1)

    # Check current state of Cursor
    existing_ports = get_used_ports()
    cursor_running = is_cursor_running()

    if cursor_running and not existing_ports:
        # Cursor is running but WITHOUT CDP. New windows would merge into
        # the existing process and our CDP flags would be ignored.
        # We can't kill Cursor automatically — user may have unsaved work.
        print("Cursor is running, but without CDP enabled.")
        print("New windows will join the existing process, so CDP cannot be added.")
        print()
        print("To fix this:")
        print("  1. Save your work and close all Cursor windows")
        print("  2. Run this script again")
        print()
        print("To avoid this in the future, always launch Cursor via this script")
        print("or add these flags to your desktop shortcut:")
        print("  --remote-debugging-port=9222 --remote-allow-origins=http://localhost:9222")
        sys.exit(1)

    if existing_ports:
        if '--port' in sys.argv:
            print(f"Note: --port is ignored when Cursor is already running (CDP on {existing_ports[0]}).")
        # Cursor already running with CDP.
        # On Windows, new windows merge into the existing process (one port).
        # On other OSes, Electron may or may not merge — we handle both:
        #   - Merged:   new page target appears on existing port
        #   - Separate: new CDP endpoint appears on new_port
        existing_port = existing_ports[0]
        new_port = find_available_port(quiet=True)
        print(f"Cursor already running with CDP on port {existing_port}.")

        before = count_page_targets(existing_port)
        print(f"Current windows: {before}")
        print(f"Launching new window...")

        # Always pass port flags so a separate process gets CDP too
        args = [
            cursor_path,
            f'--remote-debugging-port={new_port}',
            f'--remote-allow-origins=http://localhost:{new_port}',
        ]
        if sys.platform == 'win32':
            subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS)
        else:
            subprocess.Popen(args, start_new_session=True)

        # Check both possibilities: merged into existing or started separately
        print(f"Waiting for new window...", end='', flush=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            # Did it merge into the existing process?
            after = count_page_targets(existing_port)
            if after > before:
                print(f" OK! Merged into existing process ({after} windows)")
                print(f"\nCursor is ready with CDP on port {existing_port}.")
                print(f"Run: python -X utf8 pocket_cursor.py")
                return
            # Did it start as a separate process?
            if verify_cdp(new_port, timeout=0):
                print(f" OK! New instance on port {new_port}")
                print(f"\nCursor instances on ports: {existing_port}, {new_port}")
                print(f"Run: python -X utf8 pocket_cursor.py")
                return
            time.sleep(1)

        print(f" window count unchanged ({before}).")
        print(f"The window may still be loading. CDP is on port {existing_port}.")
        return

    # No Cursor running — fresh launch with CDP enabled.
    # Allow explicit port override via --port
    explicit_port = None
    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            explicit_port = int(sys.argv[idx + 1])

    MAX_RETRIES = 3
    failed_ports = set()

    for attempt in range(1, MAX_RETRIES + 1):
        if explicit_port is not None:
            port = explicit_port
        else:
            port = find_available_port(exclude=failed_ports)

        args = [
            cursor_path,
            f'--remote-debugging-port={port}',
            f'--remote-allow-origins=http://localhost:{port}',
        ]

        print(f"Launching Cursor with CDP on port {port}...")
        print(f"  {' '.join(args)}")

        if sys.platform == 'win32':
            proc = subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS)
        else:
            proc = subprocess.Popen(args, start_new_session=True)

        # Verify CDP actually came up
        print(f"Waiting for CDP on port {port}...", end='', flush=True)
        if verify_cdp(port):
            print(f" OK!")
            print(f"\nCursor is ready with CDP on port {port}.")
            print(f"Run: python -X utf8 pocket_cursor.py")
            return

        # CDP failed — kill the process and retry
        print(f" FAILED!")
        print(f"CDP did not start on port {port}. Killing process and retrying...")
        try:
            proc.kill()
        except Exception:
            pass
        failed_ports.add(port)

        if explicit_port is not None:
            print(f"Explicit port {port} failed. Cannot retry with a different port.")
            sys.exit(1)

    print(f"\nERROR: Failed to start Cursor with CDP after {MAX_RETRIES} attempts.")
    print(f"Tried ports: {sorted(failed_ports)}")
    sys.exit(1)


if __name__ == '__main__':
    main()
