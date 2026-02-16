"""
PocketCursor — Your Cursor IDE, in your pocket.

Mirrors conversations between Cursor and Telegram in both directions:
  Telegram → Cursor:  messages from your phone are typed into Cursor
  Cursor → Telegram:  AI responses stream back to your phone in real time

Connects to Cursor via Chrome DevTools Protocol (CDP).

Usage: python -X utf8 pocket_cursor.py
"""

import sys, io
if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Standard library
import atexit
import base64
import functools
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

# Sibling module
from start_cursor import get_used_ports

# Third-party
import requests
import websocket
from openai import OpenAI
from PIL import Image

print = functools.partial(print, flush=True)


# ── Config ───────────────────────────────────────────────────────────────────

env_path = Path(__file__).parent / '.env'
if env_path.exists():
    for line in env_path.read_text().strip().splitlines():
        if '=' in line and not line.startswith('#'):
            key, val = line.split('=', 1)
            os.environ[key.strip()] = val.strip()

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not set.")
    sys.exit(1)

TG_API = f"https://api.telegram.org/bot{TOKEN}"

# OpenAI API for voice transcription (optional)
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set. Voice messages won't be transcribed.")

# Owner lock: only respond to this Telegram user ID
# Set in .env or auto-captured on first /start command
OWNER_ID = os.environ.get('TELEGRAM_OWNER_ID')
OWNER_ID = int(OWNER_ID) if OWNER_ID else None
owner_file = Path(__file__).parent / '.owner_id'
chat_id_file = Path(__file__).parent / '.chat_id'

# Shared state
cdp_lock = threading.Lock()
ws = None                    # Active instance's WebSocket (all cdp_* functions use this)
instance_registry = {}       # {target_id: {workspace, ws, ws_url, title}}
active_instance_id = None    # Which instance ws points to
mirrored_chat = None         # (instance_id, pc_id, chat_name) — the ONE chat being mirrored
# Load chat_id from disk so PC messages work after restart without a Telegram message first
chat_id = int(chat_id_file.read_text().strip()) if chat_id_file.exists() else None
chat_id_lock = threading.Lock()
muted_file = Path(__file__).parent / '.muted'
muted = muted_file.exists()  # Persisted across restarts
active_chat_file = Path(__file__).parent / '.active_chat'
# Note: no reinit_monitor — monitor tracks continuously even while muted,
# just skips Telegram sends. This keeps forwarded_ids in sync at all times.
last_sent_text = None  # Last message sent by the sender thread
last_sent_lock = threading.Lock()
last_tg_message_id = None  # Message ID of the last Telegram message (for reactions)
pending_confirms = {}  # {tool_call_id: {accept_selector, reject_selector}} for inline keyboards
pending_confirms_lock = threading.Lock()


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg_call(method, **params):
    resp = requests.post(f"{TG_API}/{method}", json=params, timeout=60)
    return resp.json()


def tg_typing(cid):
    """Show 'typing...' indicator."""
    return tg_call('sendChatAction', chat_id=cid, action='typing')


def tg_send(cid, text):
    if not cid:
        return
    if len(text) <= 4000:
        return tg_call('sendMessage', chat_id=cid, text=text)
    # Split long messages at line breaks
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind('\n', 0, 4000)
        if split_at < 1000:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    if text:
        chunks.append(text)
    for chunk in chunks:
        tg_call('sendMessage', chat_id=cid, text=chunk)
        time.sleep(0.3)


def tg_escape_markdown_v2(text):
    """Escape special characters for Telegram MarkdownV2 parse mode."""
    special = r'_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + ch if ch in special else ch for ch in text)


def tg_send_thinking(cid, text):
    """Send thinking text to Telegram in italic with 💭 prefix.
    Tries MarkdownV2 italic first, falls back to plain text if formatting fails.
    """
    if not cid or not text:
        return
    # Truncate if very long (thinking can be verbose)
    if len(text) > 3500:
        cut = text[:3500].rfind('\n')
        if cut < 1000:
            cut = 3500
        text = text[:cut] + '...'
    # Try MarkdownV2 italic first
    try:
        escaped = tg_escape_markdown_v2(text)
        msg = f'_💭 {escaped}_'
        result = tg_call('sendMessage', chat_id=cid, text=msg, parse_mode='MarkdownV2')
        if result.get('ok'):
            return result
        print(f"[tg] MarkdownV2 failed: {result.get('description', '?')}, falling back to plain text")
    except Exception as e:
        print(f"[tg] MarkdownV2 error: {e}, falling back to plain text")
    # Fallback: plain text with prefix
    return tg_call('sendMessage', chat_id=cid, text=f"💭 {text}")


def tg_send_photo(cid, photo_path, caption=None):
    """Send a photo to Telegram. photo_path is a local file path."""
    if not cid or not photo_path:
        return
    try:
        with open(photo_path, 'rb') as f:
            data = {'chat_id': cid}
            if caption:
                data['caption'] = caption[:1024]  # Telegram caption limit
            resp = requests.post(f"{TG_API}/sendPhoto", data=data, files={'photo': f}, timeout=30)
            return resp.json()
    except Exception as e:
        print(f"[tg] sendPhoto error: {e}")
        return None


def tg_send_photo_bytes(cid, photo_bytes, filename='screenshot.png', caption=None):
    """Send photo from bytes (e.g. CDP screenshot)."""
    if not cid or not photo_bytes:
        return
    try:
        data = {'chat_id': cid}
        if caption:
            data['caption'] = caption[:1024]
        resp = requests.post(f"{TG_API}/sendPhoto", data=data,
                             files={'photo': (filename, photo_bytes, 'image/png')}, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[tg] sendPhoto bytes error: {e}")
        return None


def tg_send_photo_bytes_with_keyboard(cid, photo_bytes, keyboard, filename='screenshot.png', caption=None):
    """Send photo with inline keyboard buttons."""
    if not cid or not photo_bytes:
        return None
    try:
        data = {'chat_id': cid}
        if caption:
            data['caption'] = caption[:1024]
        data['reply_markup'] = json.dumps({'inline_keyboard': keyboard})
        resp = requests.post(f"{TG_API}/sendPhoto", data=data,
                             files={'photo': (filename, photo_bytes, 'image/png')}, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[tg] sendPhoto+keyboard error: {e}")
        return None


def vscode_url_to_path(url):
    """Convert vscode-file://vscode-app/c%3A/Users/... to a local file path."""
    if not url or not url.startswith('vscode-file://'):
        return None
    # Strip protocol and host: vscode-file://vscode-app/c%3A/...
    parsed = urlparse(url)
    path = unquote(parsed.path)  # decode %3A -> :
    # Remove leading / on Windows (e.g. /c:/Users -> c:/Users)
    if len(path) > 2 and path[0] == '/' and path[2] == ':':
        path = path[1:]
    # Strip query string (?t=timestamp)
    return path.split('?')[0] if '?' in path else path


def transcribe_voice(audio_bytes, filename='voice.ogg'):
    """Transcribe audio using OpenAI gpt-4o-transcribe. Returns text or None."""
    if not openai_client:
        return None
    try:
        result = openai_client.audio.transcriptions.create(
            model='gpt-4o-transcribe',
            file=(filename, audio_bytes),
        )
        return result.text
    except Exception as e:
        print(f"[transcribe] Error: {e}")
        return None


# ── CDP helpers ──────────────────────────────────────────────────────────────

def detect_cdp_port():
    """Auto-detect the CDP port from running Cursor processes.
    
    Uses start_cursor.get_used_ports() to parse process command lines,
    then verifies each port actually responds.  On Windows, merged windows
    leave ghost --remote-debugging-port entries in the launcher process's
    command line even though only the original port is bound.
    """
    ports = get_used_ports()
    if not ports:
        print("ERROR: No Cursor process with CDP detected.")
        print("Start Cursor with CDP first:  python start_cursor.py")
        print("Or check status:              python start_cursor.py --check")
        sys.exit(1)
    for port in ports:
        try:
            resp = requests.get(f'http://localhost:{port}/json', timeout=2)
            if resp.status_code == 200:
                return port
        except Exception:
            pass
    print("ERROR: Cursor process found but no CDP port is responding.")
    print(f"Ports in command line: {ports}")
    print("Start Cursor with CDP first:  python start_cursor.py")
    sys.exit(1)


def parse_instance_title(title):
    """Extract workspace name from a Cursor instance title.
    
    Title patterns:
        "Cursor"                                              → no workspace
        "file.py - WorkspaceName - Cursor"                    → "WorkspaceName"
        "file.md - Name (Workspace) - Cursor"                 → "Name (Workspace)"
        "Interactive - file.py - WorkspaceName - Cursor"      → "WorkspaceName"
    
    Workspace is always the second-to-last segment before "- Cursor".
    """
    parts = title.split(' - ')
    if len(parts) >= 3 and parts[-1].strip() == 'Cursor':
        return parts[-2]
    return None


def cdp_list_instances(port=None):
    """List all Cursor instances on the CDP port.
    
    Returns list of dicts: {id, title, workspace, ws_url}
    Instances without a workspace (e.g. "select workspace" screen) get workspace=None.
    """
    if port is None:
        port = detect_cdp_port()
    targets = requests.get(f'http://localhost:{port}/json').json()
    instances = []
    for t in targets:
        if t['type'] != 'page':
            continue
        if not t.get('url', '').startswith('vscode-file://'):
            continue
        instances.append({
            'id': t['id'],
            'title': t.get('title', ''),
            'workspace': parse_instance_title(t.get('title', '')),
            'ws_url': t['webSocketDebuggerUrl'],
        })
    return instances


def cdp_connect():
    """Connect to all Cursor instances. Sets ws to the first instance with a workspace."""
    global ws, instance_registry, active_instance_id
    port = detect_cdp_port()
    print(f"[cdp] Using port {port}")
    instances = cdp_list_instances(port)

    if not instances:
        print("ERROR: No Cursor instances found on CDP port.")
        sys.exit(1)

    # Connect to all instances, populate registry
    instance_registry.clear()
    for w in instances:
        label = w['workspace'] or '(no workspace)'
        try:
            conn = websocket.create_connection(w['ws_url'])
            instance_registry[w['id']] = {
                'workspace': w['workspace'],
                'title': w['title'],
                'ws': conn,
                'ws_url': w['ws_url'],
                'convs': {},  # {pc_id: {name, active}} — populated by overview thread
            }
            cursor_inject_tab_observer(conn)
            print(f"[cdp] Connected: {label}  [{w['id'][:8]}]")
        except Exception as e:
            print(f"[cdp] Failed to connect to {label}: {e}")

    if not instance_registry:
        print("ERROR: Could not connect to any Cursor instance.")
        sys.exit(1)

    # Scan conversations FIRST (needed for persisted state restore)
    for iid, info in instance_registry.items():
        if info['workspace']:
            try:
                convs = cursor_scan_convs(info['ws'])
                info['convs'] = {c['pc_id']: {'name': c['name'], 'active': c['active']} for c in convs}
                names = [c['name'] for c in convs]
                print(f"[cdp] Conversations in {info['workspace']}: {names}")
            except Exception:
                pass

    # Set active instance: (1) focus detection, (2) persisted state, (3) first with workspace
    active_instance_id = None

    # Try focus detection (works if user is at desk with chat input focused)
    for wid, info in instance_registry.items():
        chat = cursor_check_chat_focus(info['ws'])
        if chat:
            active_instance_id = wid
            mirrored_chat = (wid, chat['pc_id'], chat['name'])
            print(f"[cdp] Active (focused): {info['workspace']} — {chat['name']}")
            break

    # Try persisted state (works after restart from phone)
    if not active_instance_id and active_chat_file.exists():
        try:
            saved = json.loads(active_chat_file.read_text())
            saved_ws = saved.get('workspace')
            saved_pc_id = saved.get('pc_id')
            saved_name = saved.get('chat_name')
            # Match by workspace + pc_id (pc_ids survive in Cursor's DOM)
            for wid, info in instance_registry.items():
                if info['workspace'] == saved_ws:
                    for pc_id, conv in info.get('convs', {}).items():
                        if pc_id == saved_pc_id or conv['name'] == saved_name:
                            active_instance_id = wid
                            mirrored_chat = (wid, pc_id, conv['name'])
                            print(f"[cdp] Active (restored): {info['workspace']} — {conv['name']}")
                            break
                if active_instance_id:
                    break
        except Exception:
            pass

    # Fallback: first instance with a workspace
    if not active_instance_id:
        active_instance_id = next(
            (wid for wid, info in instance_registry.items() if info['workspace']),
            next(iter(instance_registry))
        )
        active_name = instance_registry[active_instance_id]['workspace'] or '(no workspace)'
        print(f"[cdp] Active (default): {active_name}")
    ws = instance_registry[active_instance_id]['ws']


msg_id_counter = 0
msg_id_lock = threading.Lock()


def cdp_eval_on(conn, expression):
    """Evaluate JS on a specific WebSocket connection. Thread-safe via cdp_lock."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        conn.send(json.dumps({
            'id': mid,
            'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'returnByValue': True}
        }))
        result = json.loads(conn.recv())
    return result.get('result', {}).get('result', {}).get('value')


def cdp_eval(expression):
    """Evaluate JS on the active instance. Thread-safe via cdp_lock."""
    return cdp_eval_on(ws, expression)


def cdp_bring_to_front(conn):
    """Bring a Cursor window to the foreground via CDP Page.bringToFront.
    
    Uses the existing WebSocket connection — no window title matching needed.
    Cross-platform: works on Windows, macOS, and Linux.
    """
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        conn.send(json.dumps({
            'id': mid,
            'method': 'Page.bringToFront',
        }))
        conn.recv()


def cdp_insert_text(text):
    """Insert text via CDP Input.insertText. Thread-safe."""
    global ws, msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        ws.send(json.dumps({
            'id': mid,
            'method': 'Input.insertText',
            'params': {'text': text}
        }))
        json.loads(ws.recv())


def cdp_screenshot():
    """Capture a screenshot of the Cursor window via CDP. Returns PNG bytes."""
    global ws, msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        ws.send(json.dumps({
            'id': mid,
            'method': 'Page.captureScreenshot',
            'params': {'format': 'png'}
        }))
        result = json.loads(ws.recv())
    b64 = result.get('result', {}).get('data')
    return base64.b64decode(b64) if b64 else None


def cdp_screenshot_element(selector):
    """Screenshot a specific DOM element by CSS selector. Returns PNG bytes or None.
    
    Takes a full screenshot (which works reliably), then crops the element
    region using Pillow. Sidesteps CDP clip coordinate/DPR issues entirely.
    """
    # Step 1: Scroll the element into view
    found = cdp_eval(f"""
        (function() {{
            const el = document.querySelector('{selector}');
            if (!el) return null;
            el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
            return 'ok';
        }})();
    """)
    if not found:
        print(f"[screenshot] Element NOT found: {selector}")
        return None

    # Step 2: Wait for scroll to settle
    time.sleep(0.5)

    # Step 3: Get bounding rect + viewport size
    rect = cdp_eval(f"""
        (function() {{
            const container = document.querySelector('{selector}');
            if (!container) return null;
            const table = container.querySelector('table.markdown-table') || container.querySelector('table') || container;
            const r = table.getBoundingClientRect();
            const pad = 6;
            return JSON.stringify({{
                x: Math.max(0, r.x - pad),
                y: Math.max(0, r.y - pad),
                width: r.width + pad * 2,
                height: r.height + pad * 2,
                viewport_w: window.innerWidth,
                viewport_h: window.innerHeight
            }});
        }})();
    """)
    if not rect:
        return None
    try:
        box = json.loads(rect)
    except (json.JSONDecodeError, TypeError):
        return None

    if box['width'] < 1 or box['height'] < 1:
        return None

    # Step 4: Take full screenshot
    full_png = cdp_screenshot()
    if not full_png:
        print("[screenshot] Full screenshot failed")
        return None

    # Step 5: Crop using Pillow — calculate scale from image size vs viewport
    img = Image.open(io.BytesIO(full_png))
    img_w, img_h = img.size
    scale_x = img_w / box['viewport_w']
    scale_y = img_h / box['viewport_h']

    # Convert CSS pixel coords to image pixel coords
    left = int(box['x'] * scale_x)
    top = int(box['y'] * scale_y)
    right = int((box['x'] + box['width']) * scale_x)
    bottom = int((box['y'] + box['height']) * scale_y)

    # Clamp to image bounds
    left = max(0, left)
    top = max(0, top)
    right = min(img_w, right)
    bottom = min(img_h, bottom)

    print(f"[screenshot] Crop: {img_w}x{img_h} @ {scale_x:.1f}x -> ({left},{top})-({right},{bottom})")

    cropped = img.crop((left, top, right, bottom))

    # Export as PNG bytes
    buf = io.BytesIO()
    cropped.save(buf, format='PNG')
    png_bytes = buf.getvalue()
    print(f"[screenshot] Result: {cropped.size[0]}x{cropped.size[1]}, {len(png_bytes)} bytes")
    return png_bytes


def cursor_paste_image(image_bytes, mime='image/png', filename='image.png'):
    """Paste an image into Cursor's editor via simulated ClipboardEvent."""
    b64 = base64.b64encode(image_bytes).decode('ascii')

    # Focus editor first
    focus_result = cdp_eval("""
        (function() {
            let editor = document.querySelector('.aislash-editor-input');
            if (!editor) {
                const all = document.querySelectorAll('[data-lexical-editor="true"]');
                for (const ed of all) {
                    if (ed.contentEditable === 'true') { editor = ed; break; }
                }
            }
            if (!editor) return 'ERROR: no editor';
            editor.focus();
            editor.click();
            return 'OK';
        })();
    """)
    if focus_result != 'OK':
        return focus_result

    time.sleep(0.3)

    # Inject image via paste event
    result = cdp_eval(f"""
        (function() {{
            const b64 = "{b64}";
            const mime = "{mime}";
            const filename = "{filename}";

            // Decode base64 to binary
            const binary = atob(b64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            const blob = new Blob([bytes], {{ type: mime }});
            const file = new File([blob], filename, {{ type: mime }});

            // Build DataTransfer with the image file
            const dt = new DataTransfer();
            dt.items.add(file);

            // Find the editor
            let editor = document.querySelector('.aislash-editor-input');
            if (!editor) {{
                const all = document.querySelectorAll('[data-lexical-editor="true"]');
                for (const ed of all) {{
                    if (ed.contentEditable === 'true') {{ editor = ed; break; }}
                }}
            }}
            if (!editor) return 'ERROR: no editor for paste';

            // Dispatch paste event
            const event = new ClipboardEvent('paste', {{
                bubbles: true,
                cancelable: true,
                clipboardData: dt
            }});
            editor.dispatchEvent(event);
            return 'OK: paste dispatched';
        }})();
    """)
    return result


# ── Cursor helpers ───────────────────────────────────────────────────────────

def cursor_click_send():
    """Click the send button in Cursor's editor. Used after image paste with no text."""
    return cdp_eval("""
        (function() {
            const selectors = [
                '.send-with-mode .anysphere-icon-button',
                'button[aria-label="Send"]',
                '.send-with-mode button',
            ];
            for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn) {
                    setTimeout(() => btn.click(), 0);
                    return 'OK: ' + sel;
                }
            }
            return 'ERROR: no send button';
        })();
    """)


def cursor_send_message(text):
    """Focus the input editor, insert text, click send.
    Holds the CDP lock for the entire sequence to avoid monitor thread contention.
    Auto-prepends [Phone] [Day YYYY-MM-DD HH:MM] to every message.
    """
    timestamp = datetime.now().strftime('%a %Y-%m-%d %H:%M')
    text = f"[{timestamp}] [Phone] {text}"

    global ws, msg_id_counter
    t0 = time.time()

    with cdp_lock:
        # 1. Focus editor
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        ws.send(json.dumps({
            'id': mid,
            'method': 'Runtime.evaluate',
            'params': {'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor) return 'ERROR: no input editor found';
                    editor.focus();
                    editor.click();
                    return 'OK';
                })();
            """, 'returnByValue': True}
        }))
        focus_result = json.loads(ws.recv())
        focus_val = focus_result.get('result', {}).get('result', {}).get('value')
        if focus_val != 'OK':
            return focus_val
        t1 = time.time()

        # 2. Insert text (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        ws.send(json.dumps({
            'id': mid,
            'method': 'Input.insertText',
            'params': {'text': text}
        }))
        json.loads(ws.recv())
        t2 = time.time()

        # 3. Verify + click send (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        ws.send(json.dumps({
            'id': mid,
            'method': 'Runtime.evaluate',
            'params': {'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor || !editor.textContent.trim()) return 'ERROR: text not inserted';
                    const selectors = [
                        '.send-with-mode .anysphere-icon-button',
                        'button[aria-label="Send"]',
                        '.send-with-mode button',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) {
                            // Async click — returns immediately, click fires on next microtask
                            setTimeout(() => btn.click(), 0);
                            return 'OK: ' + sel;
                        }
                    }
                    return 'ERROR: no send button';
                })();
            """, 'returnByValue': True}
        }))
        send_result = json.loads(ws.recv())
        result = send_result.get('result', {}).get('result', {}).get('value')

    t3 = time.time()
    print(f"[sender] Timing: focus={int((t1-t0)*1000)}ms insert={int((t2-t1)*1000)}ms verify+send={int((t3-t2)*1000)}ms total={int((t3-t0)*1000)}ms")
    return result


def cursor_get_active_conv():
    """Get the name of the active conversation tab."""
    return cdp_eval("""
        (function() {
            const tab = document.querySelector('[class*="agent-tabs"] li[class*="checked"] a[aria-id="chat-horizontal-tab"]');
            return tab ? tab.getAttribute('aria-label') : '';
        })();
    """) or ''


def cursor_list_convs():
    """List all conversation tabs. Returns [{name, active}]."""
    result = cdp_eval("""
        (function() {
            const tabs = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
            return JSON.stringify(Array.from(tabs).map((a, i) => ({
                name: a.getAttribute('aria-label') || '',
                active: a.closest('li').classList.contains('checked')
            })));
        })();
    """)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return []


def cursor_check_chat_focus(conn):
    """Detect which chat input has OS focus in this instance.

    Returns {name, pc_id} dict if a Lexical editor is focused, or None.
    Handles both agent-tabs chats and editor-group chats (split view).
    Uses document.hasFocus() so only the foreground window can return a result.
    """
    result = cdp_eval_on(conn, """
        (function() {
            if (!document.hasFocus()) return null;
            const el = document.activeElement;
            if (!el) return null;

            // Strategy 1: any focused element inside an editor-group chat (split view / file tabs)
            // Works for both Lexical editor focus (typing) and tab/content clicks
            const groups = document.querySelectorAll('.editor-group-container.has-composer-editor');
            for (const g of groups) {
                if (g.contains(el)) {
                    const tab = g.querySelector('.tab.selected .composer-tab-label') || g.querySelector('.tab .composer-tab-label');
                    const tabEl = tab ? tab.closest('.tab') : null;
                    if (tab && tabEl) return JSON.stringify({
                        name: tab.textContent.trim(),
                        pc_id: tabEl.getAttribute('data-pc-id') || ''
                    });
                }
            }

            // Strategy 2: Lexical editor focused in the main agent-tabs panel
            if (el.getAttribute('data-lexical-editor') === 'true' && el.contentEditable === 'true') {
                const checkedLi = document.querySelector('[class*="agent-tabs"] li.checked');
                if (checkedLi) {
                    const a = checkedLi.querySelector('a[aria-id="chat-horizontal-tab"]');
                    return JSON.stringify({
                        name: a ? (a.getAttribute('aria-label') || a.textContent.trim()) : '',
                        pc_id: checkedLi.getAttribute('data-pc-id') || ''
                    });
                }
            }

            return null;
        })();
    """)
    if not result or result == 'null':
        return None
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None


def cursor_inject_tab_observer(conn):
    """Inject a MutationObserver that watches for chat tab activations.

    Detects tab switches regardless of how they were triggered (tab click,
    Open Editors sidebar, keyboard shortcut, command palette, etc.) by
    observing class changes on tab elements (checked/selected).
    Idempotent — safe to call multiple times on the same instance.
    """
    cdp_eval_on(conn, """
        (function() {
            if (window.__pc_tab_observer) return 'ALREADY_INSTALLED';
            window.__pc_active_tab = null;

            const observer = new MutationObserver(mutations => {
                for (const m of mutations) {
                    if (m.attributeName !== 'class') continue;
                    const el = m.target;

                    // Agent-tab <li> became checked
                    if (el.tagName === 'LI' && el.classList.contains('checked')) {
                        const a = el.querySelector('a[aria-id="chat-horizontal-tab"]');
                        if (a) {
                            window.__pc_active_tab = {
                                name: a.getAttribute('aria-label') || a.textContent.trim(),
                                pc_id: el.getAttribute('data-pc-id') || '',
                                ts: Date.now()
                            };
                        }
                    }

                    // Editor-group tab became selected
                    if (el.classList.contains('tab') && el.classList.contains('selected')) {
                        const label = el.querySelector('.composer-tab-label');
                        if (label) {
                            window.__pc_active_tab = {
                                name: label.textContent.trim(),
                                pc_id: el.getAttribute('data-pc-id') || '',
                                ts: Date.now()
                            };
                        }
                    }

                    // Editor-group became active (lost 'inactive' class)
                    // Catches: Open Editors sidebar click, clicking into a split-view chat
                    if (el.classList.contains('editor-group-container')
                        && el.classList.contains('has-composer-editor')
                        && !el.classList.contains('inactive')) {
                        const tab = el.querySelector('.tab.selected .composer-tab-label');
                        const tabEl = tab ? tab.closest('.tab') : null;
                        if (tab && tabEl) {
                            window.__pc_active_tab = {
                                name: tab.textContent.trim(),
                                pc_id: tabEl.getAttribute('data-pc-id') || '',
                                ts: Date.now()
                            };
                        }
                    }
                }
            });

            observer.observe(document.body, { attributes: true, attributeFilter: ['class'], subtree: true });

            window.__pc_tab_observer = true;
            return 'INSTALLED';
        })();
    """)


def cursor_poll_tab_observer(conn):
    """Read and clear the latest tab activation from the MutationObserver.

    Returns {name, pc_id, ts} if a tab was activated since last poll, else None.
    """
    result = cdp_eval_on(conn, """
        (function() {
            const data = window.__pc_active_tab;
            window.__pc_active_tab = null;
            return data ? JSON.stringify(data) : null;
        })();
    """)
    if not result or result == 'null':
        return None
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None


def cursor_get_active_editor_chat(conn):
    """Get the active editor-group chat (if any) in this instance.

    Checks for a non-inactive editor-group-container that has a composer editor,
    and returns the selected tab's name and pc_id. This catches chat activations
    from any UI surface (Open Editors sidebar, keyboard shortcuts, etc.) that
    the MutationObserver might miss.

    Returns {name, pc_id} or None.
    """
    result = cdp_eval_on(conn, """
        (function() {
            const groups = document.querySelectorAll('.editor-group-container');
            for (const g of groups) {
                if (g.classList.contains('inactive')) continue;
                // Check if this group has a chat (composer editor)
                const composer = g.querySelector('[data-lexical-editor="true"]');
                if (!composer) continue;
                // Get the selected tab with a composer-tab-label
                const label = g.querySelector('.tab.selected .composer-tab-label');
                const tabEl = label ? label.closest('.tab') : null;
                if (label && tabEl) {
                    return JSON.stringify({
                        name: label.textContent.trim(),
                        pc_id: tabEl.getAttribute('data-pc-id') || ''
                    });
                }
            }
            return null;
        })();
    """)
    if not result or result == 'null':
        return None
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None


def cursor_scan_convs(conn):
    """Scan conversation tabs on a specific instance. Assigns data-pc-id to untagged tabs.
    
    Returns [{pc_id, name, active}] for all conversation tabs.
    Finds chats in two locations:
      1. Agent-tabs bar (main chat panel with horizontal tabs)
      2. Editor-group tabs (split-view: chats opened as separate editor panels)
    Uses cdp_eval_on so it works on any instance, not just the active one.
    """
    result = cdp_eval_on(conn, """
        (function() {
            const results = [];

            // 1. Agent-tabs: main chat panel with horizontal tab bar
            const agentTabs = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
            agentTabs.forEach(a => {
                const li = a.closest('li');
                if (!li.getAttribute('data-pc-id')) {
                    li.setAttribute('data-pc-id', 'pc-' + Math.random().toString(36).slice(2, 10));
                }
                results.push({
                    pc_id: li.getAttribute('data-pc-id'),
                    name: a.getAttribute('aria-label') || '',
                    active: li.classList.contains('checked')
                });
            });

            // 2. Editor-group tabs: chats in separate editor panels (split view)
            const editorGroups = document.querySelectorAll('.editor-group-container.has-composer-editor');
            editorGroups.forEach(group => {
                const tab = group.querySelector('.tab .composer-tab-label');
                if (!tab) return;
                const tabEl = tab.closest('.tab');
                if (!tabEl) return;
                const name = tab.textContent.trim();
                // Use data-resource-name as stable ID (UUID assigned by Cursor)
                const resName = tabEl.getAttribute('data-resource-name') || '';
                const pcId = 'eg-' + resName.substring(0, 8);
                // Tag the tab for click-targeting later
                if (!tabEl.getAttribute('data-pc-id')) {
                    tabEl.setAttribute('data-pc-id', pcId);
                }
                // Active = tab has the 'active' class (VS Code sets this on THE focused tab across all groups)
                // More reliable than checking group's 'inactive' class which doesn't change for panel focus
                const isActive = tabEl.classList.contains('active');
                results.push({
                    pc_id: tabEl.getAttribute('data-pc-id'),
                    name: name,
                    active: isActive
                });
            });

            return JSON.stringify(results);
        })();
    """)
    try:
        return json.loads(result) if result else []
    except (json.JSONDecodeError, TypeError):
        return []


def cursor_switch_conv(index):
    """Switch to conversation tab by 0-based index. Returns the tab name or error."""
    return cdp_eval(f"""
        (function() {{
            const tabs = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
            if ({index} >= tabs.length) return 'ERROR: only ' + tabs.length + ' tabs open';
            const tab = tabs[{index}];
            tab.click();
            return tab.getAttribute('aria-label') || 'OK';
        }})();
    """)


def cursor_get_turn_info():
    """Get the last turn's user message and all AI response sections.
    
    Uses composer-human-ai-pair-container which groups one user message
    with all its AI responses as a single turn.
    Returns individual sections (not joined) for real-time streaming.
    'turn_id' = unique DOM id of the human message (detects new turns).
    'user_full' = complete user message for forwarding to Telegram.
    'images' = list of vscode-file:// image URLs attached to the message.
    """
    result = cdp_eval("""
        (function() {
            // Helper: extract text from a markdown-section element,
            // preserving list numbering from <ol>/<li> elements.
            // textContent/innerText lose CSS-generated counters.
            function getSectionText(section) {
                let result = '';
                for (const node of section.childNodes) {
                    if (node.tagName === 'OL') {
                        node.querySelectorAll(':scope > li').forEach(li => {
                            const val = li.getAttribute('value') || '';
                            result += '\\n' + val + '. ' + li.textContent.trim();
                        });
                    } else if (node.tagName === 'UL') {
                        node.querySelectorAll(':scope > li').forEach(li => {
                            result += '\\n- ' + li.textContent.trim();
                        });
                    } else {
                        // Regular text — append inline (preserves word spacing)
                        result += node.textContent;
                    }
                }
                return result.trim();
            }

            const containers = document.querySelectorAll('.composer-human-ai-pair-container');
            if (containers.length === 0) return JSON.stringify({ turn_id: '', user_full: '', sections: [], images: [] });

            const last = containers[containers.length - 1];

            // Get the user message text from this turn
            // Use the readonly lexical editor inside the human message to avoid
            // grabbing UI elements like todo widget text
            const humanMsg = last.querySelector('[data-message-role="human"]');
            const turnId = humanMsg ? ('turn:' + (humanMsg.getAttribute('data-message-id') || '')) : '';
            let userFull = '';
            if (humanMsg) {
                const lexical = humanMsg.querySelector('.aislash-editor-input-readonly');
                userFull = lexical ? lexical.textContent.trim() : humanMsg.textContent.trim();
            }

            // Get image attachments from user message
            const images = [];
            const imgPills = last.querySelectorAll('.context-pill-image img');
            imgPills.forEach(img => {
                if (img.src) images.push(img.src);
            });

            // Get ALL content elements from AI messages in this turn, in DOM order.
            // Walks all message bubbles (AI text, tables, code blocks, tool/file-edit blocks)
            // using data-flat-index for correct ordering.
            const sections = [];
            const allBubbles = last.querySelectorAll('[data-message-role="ai"], [data-message-kind="tool"]');
            allBubbles.forEach(msg => {
                const msgId = msg.getAttribute('data-message-id') || '';
                const bubbleSuffix = msgId.split('-').pop();
                const kind = msg.getAttribute('data-message-kind');
                // Counter for generating fallback IDs when the DOM doesn't
                // provide one (tables lack a DOM id; code blocks inherit
                // from their parent markdown-section).
                let subIdx = 0;

                // --- Tool messages (file edits, confirmations, etc.) ---
                if (kind === 'tool') {
                    const toolStatus = msg.getAttribute('data-tool-status');
                    const toolCallId = msg.getAttribute('data-tool-call-id') || '';
                    const acceptBtn = msg.querySelector('.composer-run-button');
                    const rejectBtn = msg.querySelector('.composer-skip-button');

                    // Pending confirmation (action buttons visible)
                    if (toolStatus === 'loading' && acceptBtn) {
                        // Read actual button labels from the DOM
                        const acceptLabel = acceptBtn ? acceptBtn.innerText.trim().replace(/\\s+/g, ' ') : 'Accept';
                        const rejectLabel = rejectBtn ? rejectBtn.innerText.trim().replace(/\\s+/g, ' ') : 'Skip';
                        const desc = msg.querySelector('.composer-tool-former-message');
                        // Extract text from specific DOM parts, ignoring control row (buttons)
                        let cleanText = 'Action pending';
                        if (desc) {
                            const parts = [];
                            const topHeader = desc.querySelector('.composer-tool-call-top-header');
                            const header = desc.querySelector('.composer-tool-call-header');
                            const body = desc.querySelector('.composer-tool-call-body');
                            if (topHeader) parts.push(topHeader.innerText.trim().replace(/\\s+/g, ' '));
                            if (header) parts.push(header.innerText.trim().replace(/\\s+/g, ' '));
                            if (body && body.innerText.trim()) parts.push(body.innerText.trim());
                            cleanText = parts.filter(Boolean).join('\\n') || desc.innerText.trim();
                        }
                        const bubbleSelector = '#bubble-' + bubbleSuffix;
                        sections.push({
                            text: cleanText,
                            type: 'confirmation',
                            id: toolCallId || ('gen:' + msgId + ':' + subIdx),
                            selector: bubbleSelector + ' .composer-tool-former-message > div',
                            accept_selector: bubbleSelector + ' .composer-run-button',
                            reject_selector: bubbleSelector + ' .composer-skip-button',
                            accept_label: acceptLabel,
                            reject_label: rejectLabel
                        });
                        return;
                    }

                    // Completed file edit (code block with diff)
                    const codeBlock = msg.querySelector('.composer-code-block-container');
                    if (codeBlock) {
                        const filename = msg.querySelector('.composer-code-block-filename');
                        const status = msg.querySelector('.composer-code-block-status');
                        const fname = filename ? filename.textContent.trim() : 'file';
                        const stat = status ? status.textContent.trim() : '';
                        const selector = '#bubble-' + bubbleSuffix + ' .composer-code-block-container';
                        sections.push({
                            text: fname + (stat ? ' ' + stat : ''),
                            type: 'file_edit',
                            id: toolCallId || ('gen:' + msgId + ':' + subIdx),
                            selector: selector
                        });
                    }
                    return;
                }

                // --- Thinking messages ---
                if (kind === 'thinking') {
                    // Cursor removes thinking content from DOM when collapsed.
                    // If collapsed, click the header to expand so we can read
                    // the content on the next tick.
                    let root = msg.querySelector('.anysphere-markdown-container-root');
                    if (!root) {
                        const header = msg.querySelector('.collapsible-thought > div:first-child');
                        if (header) header.click();
                    }
                    // Walk sections with getSectionText to preserve list numbering.
                    // textContent/innerText lose CSS-generated <ol> counters.
                    let thinkText = '';
                    if (root) {
                        const parts = [];
                        for (const child of root.children) {
                            if (child.classList.contains('markdown-section')) {
                                const t = getSectionText(child);
                                if (t) parts.push(t);
                            }
                        }
                        thinkText = parts.join('\\n');
                    }
                    // Always push (even if empty) to hold correct index position.
                    sections.push({
                        text: thinkText,
                        type: 'thinking',
                        id: msgId || ('gen:thinking:' + subIdx),
                        selector: null
                    });
                    return;
                }

                // --- AI text messages (markdown sections, code blocks + tables) ---
                const root = msg.querySelector('.anysphere-markdown-container-root');
                if (!root) return;
                let tableIndex = 0;

                let codeBlockIndex = 0;
                for (const child of root.children) {
                    if (child.classList.contains('markdown-section')) {
                        // Code blocks live inside markdown-section but should be screenshotted
                        const codeBlock = child.querySelector('.markdown-block-code');
                        if (codeBlock) {
                            const text = child.innerText.trim();
                            const selector = '#bubble-' + bubbleSuffix +
                                ' .markdown-block-code' +
                                (codeBlockIndex > 0 ? ':nth-of-type(' + (codeBlockIndex + 1) + ')' : '');
                            sections.push({
                                text: text,
                                type: 'code_block',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                            codeBlockIndex++;
                        } else {
                            const text = getSectionText(child);
                            if (text.length > 0) {
                                sections.push({
                                    text: text,
                                    type: 'text',
                                    id: child.id || ('gen:' + msgId + ':' + subIdx),
                                    selector: null
                                });
                                subIdx++;
                            }
                        }
                    } else if (child.classList.contains('markdown-table-container')) {
                        const text = child.innerText.trim();
                        const selector = '#bubble-' + bubbleSuffix +
                            ' .markdown-table-container' +
                            (tableIndex > 0 ? ':nth-of-type(' + (tableIndex + 1) + ')' : '');
                        sections.push({
                            text: text,
                            type: 'table',
                            id: 'gen:' + msgId + ':' + subIdx,
                            selector: selector
                        });
                        subIdx++;
                        tableIndex++;
                    }
                }
            });

            // Active conversation name from the checked tab (scoped to agent-tabs to avoid terminal tabs)
            const convTab = document.querySelector('[class*="agent-tabs"] li[class*="checked"] a[aria-id="chat-horizontal-tab"]');
            const convName = convTab ? convTab.getAttribute('aria-label') : '';

            return JSON.stringify({ turn_id: turnId, user_full: userFull, sections: sections, images: images, conv: convName });
        })();
    """)
    try:
        return json.loads(result) if result else {'turn_id': '', 'user_full': '', 'sections': [], 'images': [], 'conv': ''}
    except json.JSONDecodeError:
        return {'turn_id': '', 'user_full': '', 'sections': [], 'images': [], 'conv': ''}


# ── Thread 1: Telegram → Cursor (sender) ────────────────────────────────────

def check_owner(user_id, cid):
    """Check if user_id is the owner. Auto-pair on first /start."""
    global OWNER_ID

    # Load saved owner if not set
    if OWNER_ID is None and owner_file.exists():
        OWNER_ID = int(owner_file.read_text().strip())
        print(f"[owner] Loaded owner ID: {OWNER_ID}")

    # No owner yet - accept first /start
    if OWNER_ID is None:
        return 'needs_pairing'

    return 'ok' if user_id == OWNER_ID else 'rejected'


def sender_thread():
    global chat_id, OWNER_ID, last_sent_text, last_tg_message_id, muted, active_instance_id, mirrored_chat
    print("[sender] Starting Telegram poller...")

    # Drain any pending updates from before this restart
    # so we don't re-process old messages
    offset = 0
    drain = tg_call('getUpdates', offset=0, timeout=0)
    if drain.get('ok') and drain['result']:
        offset = drain['result'][-1]['update_id'] + 1
        print(f"[sender] Skipped {len(drain['result'])} pending updates")

    while True:
        try:
            updates = tg_call('getUpdates', offset=offset, timeout=30)
            if not updates.get('ok'):
                time.sleep(2)
                continue

            for update in updates['result']:
                offset = update['update_id'] + 1

                # Handle inline keyboard callbacks (Accept/Reject)
                callback = update.get('callback_query')
                if callback:
                    cb_data = callback.get('data', '')
                    cb_id = callback.get('id')
                    cb_user_id = callback.get('from', {}).get('id')

                    # Only owner can press buttons
                    if OWNER_ID and cb_user_id != OWNER_ID:
                        continue

                    action, _, tool_id = cb_data.partition(':')
                    with pending_confirms_lock:
                        selectors = pending_confirms.pop(tool_id, None)

                    if cb_data == 'noop':
                        tg_call('answerCallbackQuery', callback_query_id=cb_id)
                        continue

                    if action in ('agent', 'chat'):
                        # New format: chat:{instance_id}:{pc_id}
                        parts = cb_data.split(':', 2)
                        if len(parts) == 3:
                            _, target_iid, target_pc_id = parts
                            info = instance_registry.get(target_iid)
                            if not info:
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Instance not found')
                                continue
                            # Click the tab with matching data-pc-id (works for both agent-tabs and editor-group tabs)
                            # Note: querySelectorAll because file tabs can share the same data-pc-id
                            # as adjacent chat tabs — we filter for the actual chat tab.
                            result = cdp_eval_on(info['ws'], f"""
                                (function() {{
                                    const candidates = document.querySelectorAll('[data-pc-id="{target_pc_id}"]');
                                    let el = null;
                                    for (const c of candidates) {{
                                        // Agent-tab: <li> with chat link
                                        if (c.querySelector('a[aria-id="chat-horizontal-tab"]')) {{ el = c; break; }}
                                        // Editor-group tab: has .composer-tab-label
                                        if (c.querySelector('.composer-tab-label')) {{ el = c; break; }}
                                    }}
                                    if (!el) return 'ERROR: tab not found (pc_id={target_pc_id}, checked ' + candidates.length + ' candidates)';
                                    // Agent-tab: click the <a> inside the <li>
                                    const a = el.querySelector('a[aria-id="chat-horizontal-tab"]');
                                    if (a) {{ a.click(); return a.getAttribute('aria-label') || 'OK'; }}
                                    // Editor-group tab: use mousedown (VS Code activates tabs on mousedown, not click)
                                    el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true, button: 0}}));
                                    const label = el.querySelector('.label-name');
                                    return label ? label.textContent.trim() || 'OK' : 'OK';
                                }})();
                            """)
                            if result and result.startswith('ERROR'):
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=result)
                            else:
                                # Switch active instance if needed
                                if target_iid != active_instance_id:
                                    with cdp_lock:
                                        active_instance_id = target_iid
                                        ws = info['ws']
                                    print(f"[sender] Switched instance to: {info['workspace']}")
                                    # Bring the target Cursor window to the foreground via CDP
                                    try:
                                        cdp_bring_to_front(info['ws'])
                                    except Exception as e:
                                        print(f"[sender] Could not bring window to front: {e}")
                                # Update mirrored_chat immediately (don't wait for overview thread)
                                chat_name = result if result and result != 'OK' else target_pc_id
                                mirrored_chat = (target_iid, target_pc_id, chat_name)
                                ws_label = (info.get('workspace') or '?').removesuffix(' (Workspace)')
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=f'Switched')
                                if chat_id:
                                    tg_send(chat_id, f"💬 Mirroring chat: {ws_label} / {chat_name}")
                                try:
                                    active_chat_file.write_text(json.dumps({
                                        'workspace': info.get('workspace'),
                                        'chat_name': chat_name,
                                        'pc_id': target_pc_id,
                                    }))
                                except Exception:
                                    pass
                            print(f"[sender] Agent switch: {result}")
                        else:
                            # Legacy format: agent:{index}
                            try:
                                idx = int(tool_id)
                            except ValueError:
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Invalid')
                                continue
                            result = cursor_switch_conv(idx)
                            if result and result.startswith('ERROR'):
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=result)
                            else:
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=f'Switched')
                            print(f"[sender] Agent switch: {result}")
                    elif selectors and action in ('accept', 'reject'):
                        btn_selector = selectors['accept'] if action == 'accept' else selectors['reject']
                        print(f"[sender] Callback: {action} tool {tool_id[:12]}...")
                        # Click the button in Cursor
                        click_result = cdp_eval(f"""
                            (function() {{
                                const btn = document.querySelector('{btn_selector}');
                                if (!btn) return 'ERROR: button not found';
                                btn.click();
                                return 'OK';
                            }})();
                        """)
                        print(f"[sender] Click result: {click_result}")
                        # Answer the callback to remove loading spinner
                        # Use the button text the user tapped (from the inline keyboard)
                        btn_text = callback.get('message', {}).get('reply_markup', {}).get(
                            'inline_keyboard', [[]])[0]
                        tapped = next((b['text'] for b in btn_text if b.get('callback_data', '').startswith(action)), None)
                        tg_call('answerCallbackQuery', callback_query_id=cb_id, text=tapped or action.capitalize())
                    else:
                        tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Expired')
                    continue

                msg = update.get('message')
                if not msg:
                    continue

                text = msg.get('text', '')
                photo = msg.get('photo')  # List of PhotoSize objects
                voice = msg.get('voice')  # Voice message object
                caption = msg.get('caption', '')

                # Skip messages with no actionable content
                if not text and not photo and not voice:
                    continue

                cid = msg['chat']['id']
                mid = msg['message_id']
                user_id = msg['from']['id']
                user = msg['from'].get('first_name', '?')

                # Owner check
                status = check_owner(user_id, cid)

                if status == 'needs_pairing':
                    # First message from anyone -> auto-pair
                    OWNER_ID = user_id
                    owner_file.write_text(str(user_id))
                    print(f"[owner] Auto-paired with {user} (ID: {user_id})")
                    tg_send(cid, "🔗 Paired! Your messages will now be forwarded to Cursor.\nSend /pause to mute, /play to resume.")

                if status == 'rejected':
                    print(f"[sender] Rejected message from {user} (ID: {user_id})")
                    tg_send(cid, "This bot is already paired with another user.")
                    continue

                # Store chat_id for the monitor thread (and persist for restarts)
                with chat_id_lock:
                    chat_id = cid
                chat_id_file.write_text(str(cid))

                # Handle photo messages (from phone gallery, camera, etc.)
                if photo:
                    print(f"[sender] {user}: [photo] {caption}")
                    tg_typing(cid)
                    # Mark so monitor knows this turn came from Telegram
                    with last_sent_lock:
                        last_sent_text = caption if caption else '[photo]'
                        last_tg_message_id = mid
                    # Get the largest resolution (last in the array)
                    file_id = photo[-1]['file_id']
                    # Download from Telegram
                    file_info = tg_call('getFile', file_id=file_id)
                    if file_info.get('ok'):
                        file_path = file_info['result']['file_path']
                        dl_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                        img_data = requests.get(dl_url, timeout=30).content
                        print(f"[sender] Downloaded {len(img_data)} bytes")

                        # Determine mime type
                        ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else 'jpg'
                        mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                                'gif': 'image/gif', 'webp': 'image/webp'}.get(ext, 'image/jpeg')

                        # Paste image into Cursor
                        paste_result = cursor_paste_image(img_data, mime, f"telegram_photo.{ext}")
                        print(f"[sender] Paste result: {paste_result}")

                        # If there's a caption, also insert it as text
                        if caption:
                            time.sleep(0.5)
                            cursor_send_message(caption)
                        else:
                            # Just click send after the image
                            time.sleep(0.5)
                            cursor_click_send()
                    else:
                        tg_send(cid, "Failed to download photo from Telegram.")
                    continue

                # Handle voice messages
                if voice:
                    print(f"[sender] {user}: [voice] {voice.get('duration', '?')}s")
                    tg_typing(cid)
                    file_id = voice['file_id']
                    file_info = tg_call('getFile', file_id=file_id)
                    if file_info.get('ok'):
                        file_path = file_info['result']['file_path']
                        dl_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                        audio_data = requests.get(dl_url, timeout=30).content
                        print(f"[sender] Downloaded voice: {len(audio_data)} bytes")

                        # Transcribe
                        transcription = transcribe_voice(audio_data)
                        if transcription:
                            print(f"[sender] Transcribed: {transcription[:80]}")
                            # Echo transcription back to Telegram so user sees what was understood
                            tg_send(cid, f"🎤 {transcription}")
                            # Send to Cursor
                            with last_sent_lock:
                                last_sent_text = transcription
                                last_tg_message_id = mid
                            result = cursor_send_message(f"[Voice] {transcription}")
                            print(f"[sender] -> Cursor: {result}")
                        else:
                            tg_send(cid, "Could not transcribe voice message. Is OPENAI_API_KEY set?")
                    else:
                        tg_send(cid, "Failed to download voice message.")
                    continue

                print(f"[sender] {user}: {text}")

                # Handle commands
                if text == '/unpair':
                    OWNER_ID = None
                    if owner_file.exists():
                        owner_file.unlink()
                    tg_send(cid, "Unpaired. Bot is now open for a new /start pairing.")
                    print(f"[owner] Unpaired")
                    continue

                if text == '/pause':
                    global muted
                    muted = True
                    muted_file.touch()
                    tg_send(cid, "⏸ Paused. Cursor messages won't be forwarded.\nSend /play to resume.")
                    print("[sender] Paused")
                    continue

                if text == '/play':
                    muted = False
                    muted_file.unlink(missing_ok=True)
                    # Include active conversation name in resume message
                    conv_name = cursor_get_active_conv()
                    resume_msg = "▶ Resumed."
                    if conv_name:
                        resume_msg += f"\n💬 {conv_name}"
                    tg_send(cid, resume_msg)
                    print("[sender] Resumed")
                    continue

                if text == '/screenshot':
                    print("[sender] Taking screenshot...")
                    png = cdp_screenshot()
                    if png:
                        tg_send_photo_bytes(cid, png, caption="Cursor IDE screenshot")
                        print(f"[sender] Screenshot sent ({len(png)} bytes)")
                    else:
                        tg_send(cid, "Failed to capture screenshot.")
                    continue

                if text in ('/chats', '/agents', '/agent'):
                    any_chats = False
                    for iid, info in instance_registry.items():
                        convs = info.get('convs', {})
                        if not convs:
                            continue
                        any_chats = True
                        ws_name = (info['workspace'] or '(no workspace)').removesuffix(' (Workspace)')
                        keyboard = []
                        for pc_id, conv in convs.items():
                            is_mirrored = mirrored_chat and mirrored_chat[0] == iid and mirrored_chat[1] == pc_id
                            prefix = '▶ ' if is_mirrored else ''
                            keyboard.append([{'text': f"{prefix}{conv['name']}", 'callback_data': f"chat:{iid}:{pc_id}"}])
                        tg_call('sendMessage', chat_id=cid, text=f'📂 {ws_name}',
                                reply_markup={'inline_keyboard': keyboard})
                    if not any_chats:
                        tg_send(cid, "No chats found.")
                    continue

                # Record what we're sending (so monitor knows which turn is ours)
                with last_sent_lock:
                    last_sent_text = text
                    last_tg_message_id = mid

                # Send to Cursor with [Phone] prefix + timestamp (day name helps resolve relative dates)
                tg_typing(cid)
                result = cursor_send_message(text)
                print(f"[sender] -> Cursor: {result}")

                if 'ERROR' in str(result):
                    tg_send(cid, f"Failed: {result}")

        except Exception as e:
            print(f"[sender] Error: {e}")
            time.sleep(2)


# ── Thread 2: Cursor → Telegram (monitor) ────────────────────────────────────

def short_id(sid):
    """Shorten section IDs for readable logs.
    'markdown-section-be9a6e9f-f29f-4a8a-b1f8-104b63383ec5-4' → '..383ec5-4'
    'gen:be9a6e9f-...:2'  → 'gen:..9f-...:2'
    Short IDs (tool call ids, etc.) are returned as-is.
    """
    if not sid or len(sid) <= 24:
        return sid or '?'
    # Show last 12 chars (captures uuid tail + section index)
    return '..' + sid[-12:]


def monitor_thread():
    print("[monitor] Starting Cursor monitor...")
    last_turn_id = None         # Track conversation turns (DOM message id)
    last_conv = None            # Track active conversation (detect tab switches)
    forwarded_ids = set()       # {section_id} — sole dedup/tracking mechanism
    sent_this_turn = False      # Whether we've forwarded anything this turn
    prev_by_id = {}             # {section_id: text} from previous tick (for stability)
    section_stable = {}         # {section_id: consecutive_stable_ticks}
    STABLE_THRESHOLD = 2        # Forward section after 2s of no change
    initialized = False
    marked_done = False         # Whether we've sent ✅ for this turn

    while True:
        try:
            time.sleep(1)

            with chat_id_lock:
                cid = chat_id
            if not cid:
                continue

            # Get the last turn's info
            turn = cursor_get_turn_info()
            turn_id = turn['turn_id']              # Unique DOM id per turn
            user_full = turn['user_full']           # Full text for forwarding
            sections = turn['sections']
            images = turn.get('images', [])
            conv = turn.get('conv', '')

            # Detect conversation tab switch → reset and skip all existing content
            if conv and last_conv is not None and conv != last_conv:
                print(f"[monitor] Conversation switched: '{last_conv[:40]}' → '{conv[:40]}', skipping {len(sections)} sections")
                # Overview thread already sends the more informative "{workspace}: {chat_name}" notification
                forwarded_ids = {
                    sec.get('id', '') for sec in sections
                    if isinstance(sec, dict) and sec.get('id')
                }
                sent_this_turn = False
                prev_by_id = {sec.get('id', ''): sec.get('text', '')
                              for sec in sections if isinstance(sec, dict) and sec.get('id')}
                section_stable = {}
                marked_done = False
                last_turn_id = turn_id
                last_conv = conv
                continue
            last_conv = conv

            if turn_id != last_turn_id:
                if not initialized:
                    print(f"[monitor] Init: '{user_full[:50]}', skipping {len(sections)} existing")
                    if not muted and conv:
                        tg_send(cid, f"💬 Connected: {conv}")
                    forwarded_ids = {
                        sec.get('id', '') for sec in sections
                        if isinstance(sec, dict) and sec.get('id')
                    }
                    initialized = True
                    last_turn_id = turn_id
                    prev_by_id = {sec.get('id', ''): sec.get('text', '')
                                  for sec in sections if isinstance(sec, dict) and sec.get('id')}
                    section_stable = {}
                    continue

                # Check if this came from Telegram or was typed directly in Cursor
                with last_sent_lock:
                    sent = last_sent_text
                # Check if the sent text appears in the user message (handles any prefix combo)
                from_telegram = (sent and (
                    sent[:30] in user_full
                    or sent == '[photo]'
                ))

                origin = "Telegram" if from_telegram else "Cursor"
                print(f"[monitor] New turn ({origin}): '{user_full[:50]}'")
                # Log all bubbles in this turn for debugging
                for idx, sec in enumerate(sections):
                    if isinstance(sec, dict):
                        print(f"  [{idx}] {sec.get('type', '?'):12s}  id={short_id(sec.get('id'))}")

                if not from_telegram:
                    # Typed directly in Cursor - forward full message
                    if not muted:
                        tg_send(cid, f"[PC] {user_full}")

                        # Forward any attached images
                        for img_url in images:
                            local_path = vscode_url_to_path(img_url)
                            if local_path and Path(local_path).exists():
                                print(f"[monitor] Forwarding image: {Path(local_path).name}")
                                tg_send_photo(cid, local_path, caption="[PC] attached image")
                            else:
                                print(f"[monitor] Image not found: {local_path}")

                # Reset tracking for all new turns
                forwarded_ids = set()
                sent_this_turn = False
                prev_by_id = {}
                section_stable = {}
                marked_done = False
                last_turn_id = turn_id
                continue

            if not initialized:
                continue

            # Keep typing indicator alive while AI is generating
            is_generating = cdp_eval("""
                (function() { return !!document.querySelector('[data-stop-button="true"]'); })();
            """)
            if is_generating and not muted:
                tg_typing(cid)

            # Log newly appeared bubbles (compare against previous tick)
            for i, sec in enumerate(sections):
                if isinstance(sec, dict) and sec.get('id'):
                    sid = sec['id']
                    if sid not in prev_by_id and sid not in forwarded_ids:
                        print(f"[monitor] + New bubble [{i}] {sec.get('type', '?'):12s}  id={short_id(sid)}")

            # Walk sections in DOM order. Skip already-forwarded IDs.
            # Stop at the first un-forwarded section that isn't stable yet
            # (preserves sequential ordering for Telegram).
            for i, sec in enumerate(sections):
                sec_key = sec.get('id', '') if isinstance(sec, dict) else ''
                text = sec['text'] if isinstance(sec, dict) else sec
                sec_type = sec.get('type', 'text') if isinstance(sec, dict) else 'text'
                sec_id = sec.get('id') if isinstance(sec, dict) else None

                # Already forwarded — skip
                if sec_key and sec_key in forwarded_ids:
                    continue

                # Check stability (keyed by ID — survives position shifts)
                prev_text = prev_by_id.get(sec_key)
                if text == prev_text:
                    section_stable[sec_key] = section_stable.get(sec_key, 0) + 1
                else:
                    section_stable[sec_key] = 0

                # Not stable yet — stop here (sequential ordering)
                if section_stable.get(sec_key, 0) < STABLE_THRESHOLD:
                    break

                # Don't forward empty thinking — wait for content to load
                if sec_type == 'thinking' and not text.strip():
                    break

                sec_selector = sec.get('selector') if isinstance(sec, dict) else None

                if sec_type == 'confirmation':
                    # Always track confirmation selectors; send keyboard only when not muted
                    tool_id = sec_id
                    with pending_confirms_lock:
                        if tool_id in pending_confirms:
                            # Already tracked this confirmation
                            if sec_key:
                                forwarded_ids.add(sec_key)
                            section_stable.pop(sec_key, None)
                            continue
                    accept_sel = sec.get('accept_selector', '')
                    reject_sel = sec.get('reject_selector', '')
                    with pending_confirms_lock:
                        pending_confirms[tool_id] = {
                            'accept': accept_sel,
                            'reject': reject_sel
                        }
                    if not muted:
                        tg_typing(cid)
                        png = None
                        if sec_selector:
                            png = cdp_screenshot_element(sec_selector)
                        accept_label = sec.get('accept_label', 'Accept')
                        reject_label = sec.get('reject_label', 'Skip')
                        keyboard = [[
                            {'text': f'✅ {accept_label}', 'callback_data': f'accept:{tool_id}'},
                            {'text': f'❌ {reject_label}', 'callback_data': f'reject:{tool_id}'}
                        ]]
                        if png:
                            print(f"[monitor] Forwarding CONFIRMATION with keyboard: {text}")
                            tg_send_photo_bytes_with_keyboard(cid, png, keyboard,
                                filename='confirmation.png', caption=f"⚡ {text}")
                        else:
                            print(f"[monitor] Forwarding CONFIRMATION as text: {text}")
                            tg_call('sendMessage', chat_id=cid, text=f"⚡ {text}",
                                    reply_markup={'inline_keyboard': keyboard})

                elif not muted:
                    # Only send to Telegram when not muted
                    tg_typing(cid)
                    if sec_type in ('table', 'file_edit', 'code_block'):
                        png = None
                        if sec_selector:
                            png = cdp_screenshot_element(sec_selector)
                        if not png and sec_type == 'table':
                            png = cdp_screenshot_element(
                                '.composer-human-ai-pair-container:last-child [data-message-role="ai"] .markdown-table-container'
                            )
                        label = {'table': 'TABLE', 'file_edit': 'FILE_EDIT', 'code_block': 'CODE_BLOCK'}[sec_type]
                        if png:
                            print(f"[monitor] Forwarding section {i+1} as {label} screenshot ({len(png)} bytes)")
                            caption = f"📝 {text}" if sec_type == 'file_edit' else ''
                            tg_send_photo_bytes(cid, png, filename=f'{sec_type}.png', caption=caption)
                        else:
                            print(f"[monitor] {label} screenshot failed, sending as text ({len(text)} chars)")
                            prefix = '📝 ' if sec_type == 'file_edit' else ''
                            tg_send(cid, f"{prefix}{text}")
                    elif sec_type == 'thinking':
                        print(f"[monitor] Forwarding THINKING ({len(text)} chars)")
                        tg_send_thinking(cid, text)
                    else:
                        print(f"[monitor] Forwarding section {i+1} ({len(text)} chars)")
                        tg_send(cid, text)

                # Always advance tracking — muted sections are "silently consumed"
                if sec_key:
                    forwarded_ids.add(sec_key)
                sent_this_turn = True
                print(f"[monitor]   → [{i}] {sec_type:12s}  id={short_id(sec_key)}  ids={len(forwarded_ids)}")
                section_stable.pop(sec_key, None)

            # Build prev_by_id for next tick's stability comparison
            prev_by_id = {}
            for sec in sections:
                if isinstance(sec, dict) and sec.get('id'):
                    prev_by_id[sec['id']] = sec.get('text', '')

            # Mark turn as done when AI finishes (for tracking)
            if sent_this_turn and not marked_done:
                is_gen = cdp_eval("""
                    (function() { return !!document.querySelector('[data-stop-button="true"]'); })();
                """)
                if not is_gen:
                    print(f"[monitor] AI done — {len(forwarded_ids)} sections forwarded")
                    marked_done = True

        except Exception as e:
            print(f"[monitor] Error: {e}")
            time.sleep(2)


# ── Overview thread ──────────────────────────────────────────────────────────
#
# Active Chat Detection Strategies
# =================================
# The overview thread detects which chat the user is interacting with using
# three complementary signals, evaluated in priority order each tick:
#
# Signal 1 — Chat input focus  (cursor_check_chat_focus)
#   Mechanism: Polls document.hasFocus() + document.activeElement for a
#              focused Lexical editor (contentEditable=true) inside a chat panel.
#   Catches:   User clicks into a chat's text input field.
#   Scope:     All instances (checks each via its own WebSocket).
#   Limitation: Only fires when the Lexical editor itself has OS focus,
#               not when the user clicks a tab or sidebar entry.
#
# Signal 2 — MutationObserver on tab classes  (cursor_inject_tab_observer / cursor_poll_tab_observer)
#   Mechanism: A MutationObserver injected once per instance watches for
#              class attribute changes on DOM elements. Three triggers:
#              (a) Agent-tab <li> gains 'checked' class → user switched chat tab
#              (b) Editor-group .tab gains 'selected' class → split-view tab switch
#              (c) Editor-group-container loses 'inactive' class → group activation
#   Catches:   Tab clicks in the agent-tabs bar, editor-group tab clicks,
#              keyboard shortcuts, command palette actions — anything that
#              causes a CSS class change on tab/group elements.
#   Scope:     All instances (observer installed per instance, polled each tick).
#   Limitation: Doesn't fire when the tab was already checked/selected and
#               the user activates it from elsewhere (e.g. Open Editors sidebar
#               click on an already-selected editor-group chat).
#
# Signal 3 — Active editor-group poll  (cursor_get_active_editor_chat)
#   Mechanism: Every tick, queries each instance for any non-inactive
#              editor-group-container that has a composer editor, and reads
#              the selected tab's name/pc_id.
#   Catches:   Open Editors sidebar clicks, any activation path that doesn't
#              trigger DOM class mutations (the "catch-all" for editor-group chats).
#   Scope:     All instances.
#   Limitation: Only detects editor-group chats (split view), not agent-tabs
#               panel chats. Those are covered by Signals 1 and 2.
#
# Together these three signals cover all known activation paths:
#   - Clicking a chat tab in agent-tabs bar      → Signal 2a
#   - Clicking into a chat's text input          → Signal 1
#   - Clicking a chat in Open Editors sidebar    → Signal 3
#   - Clicking an editor-group tab               → Signal 2b
#   - Switching to another Cursor instance       → Signal 1 or 2 (whichever fires first)
#   - Keyboard shortcut / command palette        → Signal 2
#
# Additionally, the monitor thread independently detects conversation switches
# via cursor_get_turn_info() (compares conv name each poll). This is not for
# notification but for resetting the monitor's internal forwarding state
# (forwarded_ids, prev_by_id, etc.) to avoid forwarding stale content.

FOCUS_INTERVAL = 1    # seconds between focus checks (cheap)
SCAN_INTERVAL = 3     # seconds between full rescans (heavier)

def overview_thread():
    """Periodically rescan CDP targets. Detect new/closed Cursor instances."""
    global ws, active_instance_id, mirrored_chat
    print("[overview] Starting instance monitor...")
    tick = 0
    # If mirrored_chat wasn't set by cdp_connect (no focus, no persisted state),
    # initialize from the active instance's checked tab
    if not mirrored_chat and active_instance_id and active_instance_id in instance_registry:
        info = instance_registry[active_instance_id]
        for pc_id, conv in info.get('convs', {}).items():
            if conv['active']:
                mirrored_chat = (active_instance_id, pc_id, conv['name'])
                break
    while True:
        try:
            time.sleep(FOCUS_INTERVAL)
            tick += 1
            do_full_scan = (tick % (SCAN_INTERVAL // FOCUS_INTERVAL) == 0)

            # ── Detect active chat (runs every tick) ──
            # Two signals: (1) chat input focus, (2) MutationObserver on tab classes
            # detected = (iid, pc_id, name) or None
            detected = None

            # Signal 1: which chat input has OS focus?
            for iid, info in list(instance_registry.items()):
                try:
                    chat = cursor_check_chat_focus(info['ws'])
                    if chat:
                        detected = (iid, chat['pc_id'], chat['name'])
                        break
                except Exception:
                    pass

            # Signal 2: MutationObserver — catches tab activation from any UI surface
            # (tab click, keyboard shortcut, command palette, etc.)
            if not detected:
                for iid, info in list(instance_registry.items()):
                    try:
                        tab = cursor_poll_tab_observer(info['ws'])
                        if tab:
                            detected = (iid, tab['pc_id'], tab['name'])
                            break
                    except Exception:
                        pass

            # Signal 3: poll active editor-group chat (catches Open Editors sidebar clicks
            # and any other activation path that doesn't trigger class mutations)
            if not detected:
                for iid, info in list(instance_registry.items()):
                    try:
                        eg_chat = cursor_get_active_editor_chat(info['ws'])
                        if eg_chat:
                            detected = (iid, eg_chat['pc_id'], eg_chat['name'])
                            break
                    except Exception:
                        pass

            # Apply active chat change (compare by instance + pc_id, not name)
            if detected:
                iid, pc_id, chat_name = detected
                cur_iid, cur_pc_id = (mirrored_chat[0], mirrored_chat[1]) if mirrored_chat else (None, None)
                if (iid, pc_id) != (cur_iid, cur_pc_id):
                    # Different chat — actual switch
                    mirrored_chat = (iid, pc_id, chat_name)
                    if iid != active_instance_id:
                        with cdp_lock:
                            active_instance_id = iid
                            ws = instance_registry[iid]['ws']
                    info = instance_registry.get(iid, {})
                    ws_label = (info.get('workspace') or '?').removesuffix(' (Workspace)')
                    print(f"[overview] Active: {chat_name}  in {ws_label}")
                    if chat_id:
                        tg_send(chat_id, f"💬 Mirroring chat: {ws_label} / {chat_name}")
                    try:
                        active_chat_file.write_text(json.dumps({
                            'workspace': info.get('workspace'),
                            'chat_name': chat_name,
                            'pc_id': pc_id,
                        }))
                    except Exception:
                        pass
                elif chat_name != mirrored_chat[2]:
                    # Same chat, name changed — silent rename update
                    mirrored_chat = (iid, pc_id, chat_name)
                    try:
                        active_chat_file.write_text(json.dumps({
                            'workspace': instance_registry.get(iid, {}).get('workspace'),
                            'chat_name': chat_name,
                            'pc_id': pc_id,
                        }))
                    except Exception:
                        pass

            if not do_full_scan:
                continue

            # Full scan cycle (includes focus check at the end)
            port = detect_cdp_port()
            current = cdp_list_instances(port)
            current_ids = {inst['id'] for inst in current}
            known_ids = set(instance_registry.keys())

            # Detect new instances (connect outside lock, register under lock)
            for inst in current:
                if inst['id'] not in known_ids:
                    label = inst['workspace'] or '(no workspace)'
                    try:
                        conn = websocket.create_connection(inst['ws_url'])
                        with cdp_lock:
                            instance_registry[inst['id']] = {
                                'workspace': inst['workspace'],
                                'title': inst['title'],
                                'ws': conn,
                                'ws_url': inst['ws_url'],
                                'convs': {},
                            }
                        cursor_inject_tab_observer(conn)
                        print(f"[overview] Opened: {label}  [{inst['id'][:8]}]")
                        if chat_id:
                            tg_send(chat_id, f"📂 {label}: Opened")
                    except Exception as e:
                        print(f"[overview] Failed to connect to {label}: {e}")

            # Detect closed instances
            # All registry/ws mutations under cdp_lock so sender/monitor
            # never see a closed socket or stale registry entry.
            for iid in known_ids - current_ids:
                with cdp_lock:
                    info = instance_registry.pop(iid, None)
                    if info:
                        is_active = (iid == active_instance_id)
                        if is_active and instance_registry:
                            new_id = next(
                                (k for k, v in instance_registry.items() if v['workspace']),
                                next(iter(instance_registry))
                            )
                            active_instance_id = new_id
                            ws = instance_registry[new_id]['ws']
                        elif is_active:
                            active_instance_id = None
                            ws = None
                # Close the old socket outside the lock (network I/O)
                if info:
                    label = info['workspace'] or '(no workspace)'
                    try:
                        info['ws'].close()
                    except Exception:
                        pass
                    print(f"[overview] Closed: {label}  [{iid[:8]}]")
                    if chat_id:
                        tg_send(chat_id, f"📂 {label}: Closed")
                    if is_active and active_instance_id:
                        new_name = instance_registry[active_instance_id]['workspace'] or '(no workspace)'
                        print(f"[overview] Active switched to: {new_name}")
                        if chat_id:
                            tg_send(chat_id, f"📂 {new_name}: Active")

            # Detect workspace changes (e.g. user picked a workspace in empty instance)
            for inst in current:
                if inst['id'] in instance_registry:
                    old = instance_registry[inst['id']]
                    if old['workspace'] != inst['workspace'] and inst['workspace']:
                        with cdp_lock:
                            old['workspace'] = inst['workspace']
                            old['title'] = inst['title']
                        print(f"[overview] Workspace opened: {inst['workspace']}  [{inst['id'][:8]}]")
                        if chat_id:
                            tg_send(chat_id, f"📂 {inst['workspace']}: Workspace opened")

            # Scan conversations across all instances
            for iid, info in list(instance_registry.items()):
                if not info['workspace']:
                    continue  # skip instances without a workspace
                try:
                    convs = cursor_scan_convs(info['ws'])
                except Exception:
                    continue  # WebSocket error, skip this cycle

                current_convs = {c['pc_id']: c for c in convs}
                known_convs = info['convs']
                ws_label = info['workspace']

                # New conversations
                for pc_id, conv in current_convs.items():
                    if pc_id not in known_convs:
                        print(f"[overview] New conversation: {conv['name']}  in {ws_label}")
                        if chat_id:
                            tg_send(chat_id, f"💬 {ws_label}: New chat — {conv['name']}")

                # Closed conversations
                for pc_id in set(known_convs) - set(current_convs):
                    old_name = known_convs[pc_id]['name']
                    print(f"[overview] Conversation closed: {old_name}  in {ws_label}")
                    if chat_id:
                        tg_send(chat_id, f"💬 {ws_label}: Chat closed — {old_name}")

                # Renamed conversations
                for pc_id, conv in current_convs.items():
                    if pc_id in known_convs and known_convs[pc_id]['name'] != conv['name']:
                        old_name = known_convs[pc_id]['name']
                        print(f"[overview] Conversation renamed: {old_name} → {conv['name']}  in {ws_label}")
                        if chat_id:
                            tg_send(chat_id, f"💬 {ws_label}: Chat renamed — {old_name} → {conv['name']}")

                # Update stored conversations (active chat detection handled above)
                info['convs'] = {pc_id: {'name': c['name'], 'active': c['active']} for pc_id, c in current_convs.items()}

        except Exception as e:
            print(f"[overview] Error: {e}")
            time.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────────────

# Single-instance guard: prevent multiple bridge processes
_lock_file = Path(__file__).parent / '.bridge.lock'

def _is_process_alive(pid):
    """Check if a process is truly alive (not just a stale handle) on Windows."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0400 | 0x1000, False, pid)  # PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        # GetExitCodeProcess returns STILL_ACTIVE (259) for running processes
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)

def _check_single_instance():
    """Ensure only one bridge process is running. Uses a PID lock file."""
    if _lock_file.exists():
        try:
            old_pid = int(_lock_file.read_text().strip())
            if _is_process_alive(old_pid):
                print(f"ERROR: Bridge is already running (PID {old_pid}).")
                print(f"Kill it first: taskkill /PID {old_pid} /F")
                sys.exit(1)
            # Process is dead, stale lock file — proceed
        except (ValueError, OSError):
            pass  # Corrupt or stale lock file, proceed
    # Write our PID
    _lock_file.write_text(str(os.getpid()))

def _cleanup_lock():
    try:
        if _lock_file.exists() and _lock_file.read_text().strip() == str(os.getpid()):
            _lock_file.unlink()
    except OSError:
        pass
atexit.register(_cleanup_lock)

_check_single_instance()

print("Checking bot identity...")
me = tg_call('getMe')
if not me.get('ok'):
    print("ERROR: Cannot reach Telegram API")
    sys.exit(1)
bot = me['result']
print(f"Bot: @{bot['username']} ({bot['first_name']})")

print("Connecting to Cursor via CDP...")
cdp_connect()
print("Connected.")

print(f"\nPocketCursor Bridge v2 running!")
print(f"Send a message to @{bot['username']} on Telegram.")
if OWNER_ID:
    print(f"Owner: {OWNER_ID}")
if chat_id:
    print(f"Chat ID: {chat_id} (restored from previous session)")
if muted:
    print("Status: PAUSED (restored from previous session)")
print("Press Ctrl+C to stop.\n")

t1 = threading.Thread(target=sender_thread, daemon=True)
t2 = threading.Thread(target=monitor_thread, daemon=True)
t3 = threading.Thread(target=overview_thread, daemon=True)
t1.start()
t2.start()
t3.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nStopping...")
    for info in instance_registry.values():
        try:
            info['ws'].close()
        except Exception:
            pass
    print("Done.")
