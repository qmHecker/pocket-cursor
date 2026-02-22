"""Set up a local render directory for fast phone outbox rendering.

If the repo lives on a network drive with high latency (e.g. NAS over VPN),
rendering can be slow because Node.js loads hundreds of small files from
node_modules on every run. This script creates a local copy of the render
tooling so all file I/O stays on a fast local disk.

Usage:
    python setup_local_render.py              # reads RENDER_LOCAL_DIR from .env
    python setup_local_render.py C:\my\path   # explicit path

After running, add this to your .env (if not already there):
    RENDER_LOCAL_DIR=<the path you chose>
"""
import sys, os, shutil, subprocess as sp
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pathlib import Path
from dotenv import load_dotenv

REPO_DIR = Path(__file__).parent
load_dotenv(REPO_DIR / '.env')

target = None
if len(sys.argv) > 1:
    target = Path(sys.argv[1])
else:
    env_val = os.environ.get('RENDER_LOCAL_DIR', '').strip()
    if env_val:
        target = Path(env_val)

if not target:
    print("No target directory specified.")
    print("Either pass a path as argument or set RENDER_LOCAL_DIR in .env")
    print(f"  Example: python {Path(__file__).name} Q:\\.pocket-cursor\\render")
    sys.exit(1)

script_src = REPO_DIR / 'md_to_image.mjs'
if not script_src.exists():
    print(f"ERROR: {script_src} not found")
    sys.exit(1)

print(f"Setting up local render directory:")
print(f"  Source:  {REPO_DIR}")
print(f"  Target:  {target}")
print()

target.mkdir(parents=True, exist_ok=True)

print("[1/3] Copying md_to_image.mjs ...")
shutil.copy2(script_src, target / 'md_to_image.mjs')

print("[2/3] Installing Node.js dependencies ...")
npm_init = sp.run(['npm', 'init', '-y'], cwd=str(target), shell=True,
                  capture_output=True, text=True, encoding='utf-8', errors='replace')
if npm_init.returncode != 0:
    print(f"  npm init failed: {npm_init.stderr.strip()}")
    sys.exit(1)

npm_install = sp.run(['npm', 'install', 'puppeteer', 'marked'], cwd=str(target), shell=True,
                     capture_output=True, text=True, encoding='utf-8', errors='replace',
                     timeout=300)
if npm_install.returncode != 0:
    print(f"  npm install failed: {npm_install.stderr.strip()}")
    sys.exit(1)
print("  Done.")

print("[3/3] Test render ...")
test_md = target / '_test.md'
test_png = target / '_test.png'
test_md.write_text("# Test\n\nIf you see this image, local rendering works.", encoding='utf-8')

import time
t0 = time.perf_counter()
render = sp.run(
    ['node', str(target / 'md_to_image.mjs'), str(test_md), '--out', str(test_png)],
    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120
)
elapsed = time.perf_counter() - t0

test_md.unlink(missing_ok=True)
if render.returncode != 0:
    print(f"  Render failed: {render.stderr.strip()}")
    test_png.unlink(missing_ok=True)
    sys.exit(1)

size_kb = test_png.stat().st_size / 1024
test_png.unlink(missing_ok=True)
print(f"  Rendered in {elapsed:.1f}s ({size_kb:.0f} KB)")

print()
print("Setup complete. Make sure your .env contains:")
print(f"  RENDER_LOCAL_DIR={target}")
