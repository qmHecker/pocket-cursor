#!/usr/bin/env python3
"""
start_cursor.py
Find and launch Cursor IDE with CDP (Chrome DevTools Protocol) enabled.

This is required for PocketCursor to communicate with Cursor.
Run this instead of launching Cursor normally, or add the flags to your
existing shortcut.

Usage:
    python start_cursor.py
"""

import subprocess
import sys
import os
from pathlib import Path


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

    args = [
        cursor_path,
        '--remote-debugging-port=9222',
        '--remote-allow-origins=http://localhost:9222',
    ]

    print(f"Launching Cursor with CDP enabled...")
    print(f"  {' '.join(args)}")
    print()
    print("Once Cursor is open, run: python -X utf8 pocket_cursor.py")

    # Launch detached so this script can exit
    if sys.platform == 'win32':
        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS)
    else:
        subprocess.Popen(args, start_new_session=True)


if __name__ == '__main__':
    main()
