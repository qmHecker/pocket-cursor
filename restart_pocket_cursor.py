#!/usr/bin/env python3
"""
restart_pocket_cursor.py
Kill any running PocketCursor processes and start a fresh instance.

Why a single script instead of "kill + start" as two commands?
If Cursor requires approval before running commands, killing
PocketCursor first would leave you unable to approve the start
command from your phone. This script does both in one approval —
kill the old process and immediately start a new one.

Usage:
    python restart_pocket_cursor.py
"""

import subprocess
import sys
import os
import functools
from pathlib import Path

print = functools.partial(print, flush=True)

SCRIPT_DIR = Path(__file__).parent
POCKET_CURSOR_SCRIPT = SCRIPT_DIR / 'pocket_cursor.py'


def find_pids():
    """Find PIDs of running PocketCursor processes via wmic."""
    try:
        result = subprocess.run(
            ['wmic', 'process', 'where',
             "commandline like '%pocket_cursor.py%' and not commandline like '%wmic%' and not commandline like '%restart%'",
             'get', 'processid'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        pids = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception as e:
        print(f"Warning: Could not query processes: {e}")
        return []


def kill_processes(pids):
    """Kill PocketCursor processes with /T (process tree)."""
    for pid in pids:
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10
            )
            print(f"  Killed PID {pid}: {result.stdout.strip()}")
        except Exception as e:
            print(f"  Failed to kill PID {pid}: {e}")


def main():
    # 1. Find and kill existing PocketCursor processes
    pids = find_pids()
    if pids:
        print(f"Found {len(pids)} PocketCursor process(es): {pids}")
        kill_processes(pids)
    else:
        print("No running PocketCursor found.")

    # 2. Clean stale lock file
    lock_file = SCRIPT_DIR / '.bridge.lock'
    if lock_file.exists():
        lock_file.unlink()
        print("Removed stale lock file.")

    # 3. Start PocketCursor as subprocess and wait (keeps parent alive so
    #    Cursor terminal tracking is preserved — os.execv on Windows
    #    spawns a new process and exits, which orphans it).
    print(f"\nStarting PocketCursor from {SCRIPT_DIR}...")
    os.chdir(SCRIPT_DIR)
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(POCKET_CURSOR_SCRIPT)])
    sys.exit(proc.wait())


if __name__ == '__main__':
    main()
