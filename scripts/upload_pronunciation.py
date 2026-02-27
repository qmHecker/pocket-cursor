#!/usr/bin/env python3
"""
Upload a PLS pronunciation dictionary to ElevenLabs.

Usage:
    python scripts/upload_pronunciation.py scripts/pronunciation.example.pls

Prints the dictionary ID and version ID to add to your .env file.
"""

import sys
import io
import functools
from pathlib import Path

if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer') and getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

print = functools.partial(print, flush=True)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass

import os
import requests

KEY = os.environ.get('ELEVENLABS_API_KEY', '')
if not KEY:
    print('Error: ELEVENLABS_API_KEY not set in .env')
    sys.exit(1)

if len(sys.argv) < 2:
    print('Usage: python scripts/upload_pronunciation.py <path-to-pls-file>')
    sys.exit(1)

pls_path = Path(sys.argv[1])
if not pls_path.exists():
    print(f'File not found: {pls_path}')
    sys.exit(1)

pls_content = pls_path.read_text(encoding='utf-8')
name = pls_path.stem

resp = requests.post(
    'https://api.elevenlabs.io/v1/pronunciation-dictionaries/add-from-file',
    headers={'xi-api-key': KEY},
    data={'name': name},
    files={'file': (pls_path.name, pls_content.encode('utf-8'), 'text/xml')},
)

if resp.status_code not in (200, 201):
    print(f'Error {resp.status_code}: {resp.text[:300]}')
    sys.exit(1)

d = resp.json()
print(f'Uploaded successfully!\n')
print(f'Add these to your .env:')
print(f'ELEVENLABS_PRONUNCIATION_DICT_ID={d["id"]}')
print(f'ELEVENLABS_PRONUNCIATION_DICT_VERSION={d["version_id"]}')
