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
import json
import os
import re
import subprocess as sp
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

# Sibling modules
from start_cursor import get_used_ports
from chat_detection import install_chat_listener, start_chat_listener, list_chats, ts_print

# Third-party
import requests
import websocket
from openai import OpenAI
from PIL import Image

print = ts_print


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

# Context journal: prefill annotation into chat input when context window is filling up
CONTEXT_MONITOR = os.environ.get('CONTEXT_MONITOR', '').lower() in ('true', '1', 'yes')

# Owner lock: only respond to this Telegram user ID
# Set in .env or auto-captured on first /start command
OWNER_ID = os.environ.get('TELEGRAM_OWNER_ID')
OWNER_ID = int(OWNER_ID) if OWNER_ID else None
owner_file = Path(__file__).parent / '.owner_id'
chat_id_file = Path(__file__).parent / '.chat_id'

# Shared state
cdp_lock = threading.Lock()
ws = None                    # Active instance's WebSocket (all cdp_* functions use this)
_browser_ws_url = None       # Browser-level WebSocket URL (cached at connect time)
instance_registry = {}       # {target_id: {workspace, ws, ws_url, title}}
active_instance_id = None    # Which instance ws points to
mirrored_chat = None         # (instance_id, pc_id, chat_name) — the ONE chat being mirrored
# Load chat_id from disk so PC messages work after restart without a Telegram message first
chat_id = int(chat_id_file.read_text().strip()) if chat_id_file.exists() else None
chat_id_lock = threading.Lock()
muted_file = Path(__file__).parent / '.muted'
muted = muted_file.exists()  # Persisted across restarts
active_chat_file = Path(__file__).parent / '.active_chat'
context_pcts_file = Path(__file__).parent / '.context_pcts'
phone_outbox = Path(__file__).parent / '_phone_outbox'
# Note: no reinit_monitor — monitor tracks continuously even while muted,
# just skips Telegram sends. This keeps forwarded_ids in sync at all times.
last_sent_text = None  # Last message sent by the sender thread
last_sent_lock = threading.Lock()
last_tg_message_id = None  # Message ID of the last Telegram message (for reactions)
pending_confirms = {}  # {tool_call_id: {buttons_selector, buttons: [{label, index}]}} for inline keyboards
pending_confirms_lock = threading.Lock()


def _save_active_chat(workspace, chat_name, pc_id):
    """Persist active chat state."""
    try:
        active_chat_file.write_text(json.dumps({
            'workspace': workspace,
            'chat_name': chat_name,
            'pc_id': pc_id,
        }))
    except Exception:
        pass


# ── Telegram helpers ─────────────────────────────────────────────────────────

def tg_call(method, **params):
    resp = requests.post(f"{TG_API}/{method}", json=params, timeout=60)
    result = resp.json()
    if not result.get('ok'):
        desc = result.get('description', '?')
        code = result.get('error_code', '?')
        print(f"[telegram] API error: {method} -> {code} {desc}")
    return result


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
        print(f"[telegram] MarkdownV2 failed: {result.get('description', '?')}, falling back to plain text")
    except Exception as e:
        print(f"[telegram] MarkdownV2 error: {e}, falling back to plain text")
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
            result = resp.json()
            if not result.get('ok'):
                desc = result.get('description', '?')
                code = result.get('error_code', '?')
                print(f"[telegram] sendPhoto failed: {code} {desc}  ({photo_path})")
            return result
    except Exception as e:
        print(f"[telegram] sendPhoto error: {e}")
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
        result = resp.json()
        if not result.get('ok'):
            desc = result.get('description', '?')
            code = result.get('error_code', '?')
            print(f"[telegram] sendPhoto failed: {code} {desc}  ({len(photo_bytes)} bytes)")
        return result
    except Exception as e:
        print(f"[telegram] sendPhoto bytes error: {e}")
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
        result = resp.json()
        if not result.get('ok'):
            desc = result.get('description', '?')
            code = result.get('error_code', '?')
            print(f"[telegram] sendPhoto+keyboard failed: {code} {desc}  ({len(photo_bytes)} bytes)")
        return result
    except Exception as e:
        print(f"[telegram] sendPhoto+keyboard error: {e}")
        return None


POCKET_CURSOR_COMMANDS = [
    {'command': 'newchat', 'description': 'Start a new chat in Cursor'},
    {'command': 'chats', 'description': 'Show all chats across instances'},
    {'command': 'pause', 'description': 'Pause Cursor to Telegram forwarding'},
    {'command': 'play', 'description': 'Resume forwarding'},
    {'command': 'screenshot', 'description': 'Screenshot your Cursor window'},
    {'command': 'unpair', 'description': 'Disconnect this device'},
]


def tg_commands_need_update():
    """Check if bot commands are missing or outdated compared to POCKET_CURSOR_COMMANDS."""
    try:
        existing = tg_call('getMyCommands')
        current = existing.get('result', []) if existing.get('ok') else []
        registered = {c['command']: c['description'] for c in current}
        for cmd in POCKET_CURSOR_COMMANDS:
            if cmd['command'] not in registered:
                return True
            if registered[cmd['command']] != cmd['description']:
                return True
        return False
    except Exception:
        return False


def tg_register_commands():
    """Merge PocketCursor commands into existing bot commands (doesn't overwrite others)."""
    try:
        existing = tg_call('getMyCommands')
        current = existing.get('result', []) if existing.get('ok') else []
        our_names = {c['command'] for c in POCKET_CURSOR_COMMANDS}
        merged = [c for c in current if c['command'] not in our_names]
        merged.extend(POCKET_CURSOR_COMMANDS)
        result = tg_call('setMyCommands', commands=merged)
        ok = result.get('ok', False)
        print(f"[telegram] Registered {len(POCKET_CURSOR_COMMANDS)} commands (total {len(merged)}): {'OK' if ok else result}")
        return ok
    except Exception as e:
        print(f"[telegram] Failed to register commands: {e}")
        return False


def tg_ask_command_update(cid):
    """Send an inline keyboard asking the user to update bot commands."""
    tg_call('sendMessage', chat_id=cid,
            text="New commands available. Want me to update your Telegram bot menu?",
            reply_markup={'inline_keyboard': [
                [{'text': '✅ Yes, update', 'callback_data': 'setup_commands:yes'},
                 {'text': 'Skip', 'callback_data': 'setup_commands:no'}]
            ]})


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
        if t.get('url', '').startswith('devtools://'):
            continue
        instances.append({
            'id': t['id'],
            'title': t.get('title', ''),
            'workspace': parse_instance_title(t.get('title', '')),
            'ws_url': t['webSocketDebuggerUrl'],
        })
    return instances


# ── Chat listener callbacks ───────────────────────────────────────────────────

_switch_lock = threading.Lock()
_switch_debounce_lock = threading.Lock()
_switch_debounce_timer = None
_switch_debounce_pre_pcid = None

def _handle_chat_switch(iid, data):
    """Called by chat listener thread when user switches to a different chat.

    Debounces Telegram notifications (1.5s) to suppress rapid focus bounces
    that happen when Cursor moves a chat between sidebar and editor views.
    If the focus returns to the original chat (A->B->A), no notification is sent.
    """
    global mirrored_chat, active_instance_id, ws
    global _switch_debounce_timer, _switch_debounce_pre_pcid
    pc_id = data.get('pc_id', '')
    name = data.get('name', '')
    if not pc_id:
        return
    with _switch_lock:
        cur_iid = mirrored_chat[0] if mirrored_chat else None
        cur_pc_id = mirrored_chat[1] if mirrored_chat else None
        if iid == cur_iid and pc_id == cur_pc_id:
            return
        same_chat_new_window = (pc_id == cur_pc_id and iid != cur_iid)
        mirrored_chat = (iid, pc_id, name)
    if iid != active_instance_id:
        with cdp_lock:
            active_instance_id = iid
            if iid in instance_registry:
                ws = instance_registry[iid]['ws']
    info = instance_registry.get(iid, {})
    ws_label = (info.get('workspace') or '?').removesuffix(' (Workspace)')
    is_provisional = pc_id.startswith('pc-')
    print(f"[dom] Active: {name}  in {ws_label}" + (" (provisional)" if is_provisional else ""))
    # Seed the overview's known_convs so it can detect renames.
    # Without this, a new chat created and auto-renamed before the next
    # overview scan would never be seen under its original name ("New Chat").
    if not is_provisional and 'convs' in info and pc_id not in info['convs']:
        info['convs'][pc_id] = {'name': name, 'active': True, 'msg_id': None}
    if chat_id and not muted and not is_provisional and not same_chat_new_window:
        with _switch_debounce_lock:
            if _switch_debounce_timer:
                _switch_debounce_timer.cancel()
            else:
                _switch_debounce_pre_pcid = cur_pc_id

            def _fire(n=name, wsl=ws_label, pid=pc_id):
                global _switch_debounce_timer, _switch_debounce_pre_pcid
                with _switch_debounce_lock:
                    _switch_debounce_timer = None
                    pre = _switch_debounce_pre_pcid
                    _switch_debounce_pre_pcid = None
                if pre == pid:
                    print(f"[dom] Suppressed notification (returned to same chat: {n})")
                    return
                try:
                    tg_send(chat_id, f"\U0001f4ac Chat activated: {n}  ({wsl})")
                except Exception:
                    pass

            _switch_debounce_timer = threading.Timer(1.5, _fire)
            _switch_debounce_timer.start()
    _save_active_chat(info.get('workspace'), name, pc_id)
    if CONTEXT_MONITOR:
        try:
            cursor_clear_input()
        except Exception:
            pass


def _handle_chat_rename(iid, data):
    """Called by chat listener thread when active chat's name changes."""
    global mirrored_chat
    pc_id = data.get('pc_id', '')
    name = data.get('name', '')
    if not pc_id or not name:
        return
    if mirrored_chat and mirrored_chat[0] == iid and mirrored_chat[1] == pc_id:
        mirrored_chat = (iid, pc_id, name)
        info = instance_registry.get(iid, {})
        _save_active_chat(info.get('workspace'), name, pc_id)
    if CONTEXT_MONITOR and pc_id in _context_pct_names:
        _context_pct_names[pc_id] = name
        _save_context_pcts(pc_id=pc_id, chat_name=name)


def _on_listener_dead(label, exc):
    """Called when a chat listener thread dies. Flags the instance for reconnect."""
    for iid, info in instance_registry.items():
        ws_label = info.get('workspace') or '(no workspace)'
        if ws_label == label or label == ws_label:
            info['listener_dead'] = True
            print(f"[overview] Listener dead for {label}, will reconnect on next scan")
            return


def _setup_chat_listener(iid, ws_url, label):
    """Open a dedicated listener WebSocket and start the chat listener thread."""
    listener_conn = websocket.create_connection(ws_url)
    install_chat_listener(listener_conn)
    start_chat_listener(
        listener_conn, label,
        on_switch=lambda data: _handle_chat_switch(iid, data),
        on_rename=lambda data: _handle_chat_rename(iid, data),
        on_dead=_on_listener_dead,
    )
    return listener_conn


# ── Context Journal Monitor ──────────────────────────────────────────────────
# Reads the context window fill level from the SVG token ring in Cursor's DOM.
# The monitor thread sends a follow-up annotation message when the threshold
# is crossed or a summary is detected.

_CONTEXT_PCT_JS = """
(function() {
    var c = document.querySelector('.token-ring-progress');
    if (!c) return null;
    var total = parseFloat(c.getAttribute('stroke-dasharray'));
    var off = parseFloat(c.getAttribute('stroke-dashoffset'));
    if (!total || isNaN(off)) return null;
    return Math.round((1 - off / total) * 1000) / 10;
})()
"""


def get_context_pct(conn=None):
    """Read the context window fill % from the active Cursor instance."""
    try:
        result = cdp_eval_on(conn, _CONTEXT_PCT_JS) if conn else cdp_eval(_CONTEXT_PCT_JS)
        return float(result) if result is not None else None
    except (TypeError, ValueError):
        return None


def _build_context_annotation(ctx, pc_id):
    """Build the annotation string, or None if no annotation needed."""
    if ctx is None:
        return None
    prev = _context_pcts.get(pc_id)
    hint = "(see pocket-cursor.mdc § Context monitor)"
    if prev is not None and prev - ctx > 5:
        return (f"[ContextMonitor: context was summarized "
                f"({int(prev)}% -> {int(ctx)}%) -- check your journal {hint}]")
    if ctx >= CONTEXT_MONITOR_THRESHOLD:
        return f"[ContextMonitor: {int(ctx)}% context used -- journal reminder {hint}]"
    return None


def cdp_connect():
    """Connect to all Cursor instances. Restores the last active chat from .active_chat, or defaults to the first instance with a workspace."""
    global ws, instance_registry, active_instance_id, mirrored_chat, _browser_ws_url
    port = detect_cdp_port()
    print(f"[cdp] Using port {port}")
    try:
        binfo = requests.get(f'http://localhost:{port}/json/version', timeout=3).json()
        _browser_ws_url = binfo.get('webSocketDebuggerUrl')
        print(f"[cdp] Browser WS: {_browser_ws_url}")
    except Exception:
        _browser_ws_url = None
    instances = cdp_list_instances(port)

    if not instances:
        print("ERROR: No Cursor instances found on CDP port.")
        sys.exit(1)

    instance_registry.clear()
    for w in instances:
        label = w['workspace'] or '(no workspace)'
        try:
            conn = websocket.create_connection(w['ws_url'])
            listener_conn = _setup_chat_listener(w['id'], w['ws_url'], label)
            instance_registry[w['id']] = {
                'workspace': w['workspace'],
                'title': w['title'],
                'ws': conn,
                'ws_url': w['ws_url'],
                'listener_ws': listener_conn,
                'convs': {},
            }
            print(f"[cdp] Connected: {label}  [{w['id'][:8]}]")
        except Exception as e:
            print(f"[cdp] Failed to connect to {label}: {e}")

    if not instance_registry:
        print("ERROR: Could not connect to any Cursor instance.")
        sys.exit(1)

    for iid, info in instance_registry.items():
        if info['workspace']:
            try:
                convs = list_chats(lambda js, c=info['ws']: cdp_eval_on(c, js))
                info['convs'] = {c['pc_id']: {'name': c['name'], 'active': c['active'], 'msg_id': c.get('msg_id')} for c in convs}
                names = [c['name'] for c in convs]
                print(f"[cdp] Conversations in {info['workspace']}: {names}")
            except Exception:
                pass

    # Set active instance: (1) persisted state, (2) first with workspace
    active_instance_id = None
    mirrored_chat = None

    if active_chat_file.exists():
        try:
            saved = json.loads(active_chat_file.read_text())
            saved_ws = saved.get('workspace')
            saved_pc_id = saved.get('pc_id')
            saved_name = saved.get('chat_name')
            for wid, info in instance_registry.items():
                if info['workspace'] == saved_ws:
                    for pc_id, conv in info.get('convs', {}).items():
                        if pc_id == saved_pc_id or conv['name'] == saved_name:
                            active_instance_id = wid
                            mirrored_chat = (wid, pc_id, conv['name'])
                            print(f"[cdp] Active (restored): {info['workspace']} -- {conv['name']}")
                            break
                if active_instance_id:
                    break
        except Exception:
            pass

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


def active_conn():
    """Return the WebSocket for the active instance (from registry, not the global ws)."""
    if active_instance_id and active_instance_id in instance_registry:
        return instance_registry[active_instance_id]['ws']
    return ws


def cdp_eval(expression):
    """Evaluate JS on the active instance. Thread-safe via cdp_lock."""
    return cdp_eval_on(active_conn(), expression)


def _cdp_cmd(conn, method, params=None):
    """Send a CDP command and return the result. Thread-safe."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    msg = {'id': mid, 'method': method}
    if params:
        msg['params'] = params
    with cdp_lock:
        conn.send(json.dumps(msg))
        return json.loads(conn.recv())


def _win32_force_foreground(title):
    """Bypass Windows focus-stealing prevention.

    Primary: SetWindowPos with HWND_TOPMOST (no flicker, z-order trick).
    Fallback: minimize/restore (flickers but always works).
    """
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32

    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        print(f"[cdp] bring_to_front: FindWindowW no match for '{title[:50]}'")
        return False

    # Properly typed HWND values — critical on 64-bit Windows where
    # HWND is a pointer (c_void_p). Without argtypes, ctypes truncates
    # -1 to 32-bit c_int which SetWindowPos silently ignores.
    user32.SetWindowPos.argtypes = [
        wt.HWND, wt.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wt.BOOL

    SWP = 0x0002 | 0x0001 | 0x0040  # NOMOVE | NOSIZE | SHOWWINDOW
    TOPMOST = wt.HWND(-1)
    NOTOPMOST = wt.HWND(-2)

    r1 = user32.SetWindowPos(hwnd, TOPMOST, 0, 0, 0, 0, SWP)
    r2 = user32.SetWindowPos(hwnd, NOTOPMOST, 0, 0, 0, 0, SWP)
    user32.SetForegroundWindow(hwnd)

    if r1 and r2:
        print(f"[cdp] bring_to_front: SetWindowPos OK  hwnd={hwnd}  title='{title[:50]}'")
        return True

    # Fallback: minimize/restore (causes brief flicker but guaranteed)
    print(f"[cdp] bring_to_front: SetWindowPos failed ({r1},{r2}), trying minimize/restore")
    user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
    time.sleep(0.05)
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    print(f"[cdp] bring_to_front: minimize/restore  hwnd={hwnd}")
    return True


def cdp_bring_to_front(conn, target_id=None):
    """Bring a Cursor window to the foreground.

    Three-stage strategy:
      1. CDP Page.bringToFront + window.focus() (works when OS allows it)
      2. Target.activateTarget on browser-level WS (activates the tab in Chrome)
      3. OS-specific: Win32 SetWindowPos | macOS osascript | Linux xdotool
    """
    print(f"[cdp] bring_to_front: Page.bringToFront + window.focus()  target={target_id}")
    _cdp_cmd(conn, 'Page.bringToFront')
    cdp_eval_on(conn, 'window.focus()')

    if target_id and _browser_ws_url:
        try:
            browser_conn = websocket.create_connection(_browser_ws_url)
            try:
                browser_conn.send(json.dumps({
                    'id': 1, 'method': 'Target.activateTarget',
                    'params': {'targetId': target_id}
                }))
                result = json.loads(browser_conn.recv())
                if result.get('error'):
                    print(f"[cdp] bring_to_front: Target.activateTarget FAILED: {result['error']}")
                else:
                    print(f"[cdp] bring_to_front: Target.activateTarget OK  target={target_id[:8]}")
            finally:
                browser_conn.close()
        except Exception as e:
            print(f"[cdp] bring_to_front: Target.activateTarget exception: {e}")

    try:
        title = cdp_eval_on(conn, 'document.title')
        if not title:
            print(f"[cdp] bring_to_front: document.title was empty")
        elif sys.platform == 'win32':
            _win32_force_foreground(title)
        elif sys.platform == 'darwin':
            sp.Popen(['osascript', '-e', 'tell application "Cursor" to activate'],
                      stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            print(f"[cdp] bring_to_front: osascript activate")
        elif sys.platform.startswith('linux'):
            sp.Popen(['xdotool', 'search', '--name', title, 'windowactivate'],
                      stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            print(f"[cdp] bring_to_front: xdotool windowactivate")
    except Exception as e:
        print(f"[cdp] bring_to_front: OS fallback exception: {e}")


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


def cdp_screenshot_on(conn):
    """Capture a screenshot via CDP on a specific connection. Returns PNG bytes."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        conn.send(json.dumps({
            'id': mid,
            'method': 'Page.captureScreenshot',
            'params': {'format': 'png'}
        }))
        result = json.loads(conn.recv())
    b64 = result.get('result', {}).get('data')
    return base64.b64decode(b64) if b64 else None


def cdp_screenshot():
    """Capture a screenshot of the active Cursor window. Returns PNG bytes."""
    return cdp_screenshot_on(active_conn())


def cdp_hover_file_path(filename_selector):
    """Hover over a filename element in the chat to read the full path from its tooltip.

    Uses CDP Input.dispatchMouseEvent (synthetic, doesn't move the real cursor).
    Tooltip format: 'workspace • relative\\path\\file.ext'
    Returns the relative path (e.g., 'scripts/food-tracker/journal.md') or None.
    """
    try:
        conn = active_conn()
        pos = cdp_eval_on(conn, f"""
            (() => {{
                const el = document.querySelector('{filename_selector}');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
            }})();
        """)
        if not pos:
            return None
        box = json.loads(pos)

        # Hover over filename to trigger tooltip
        _cdp_cmd(conn, 'Input.dispatchMouseEvent', {
            'type': 'mouseMoved',
            'x': int(box['x']),
            'y': int(box['y'])
        })

        # Poll for tooltip (typically appears within 50-100ms)
        tooltip = None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            tooltip = cdp_eval_on(conn, """
                (() => {
                    const hover = document.querySelector('.workbench-hover-container .hover-contents');
                    return hover ? hover.textContent.trim() : null;
                })();
            """)
            if tooltip:
                break
            time.sleep(0.05)

        # Move mouse away to dismiss tooltip
        _cdp_cmd(conn, 'Input.dispatchMouseEvent', {
            'type': 'mouseMoved',
            'x': 0, 'y': 0
        })
        time.sleep(0.1)

        if not tooltip:
            return None
        # "workspace • relative\path\file.ext" → "relative/path/file.ext"
        parts = tooltip.split(' • ', 1)
        if len(parts) == 2:
            return parts[1].replace('\\', '/')
        return tooltip.replace('\\', '/')
    except Exception as e:
        print(f"[monitor] cdp_hover_file_path error: {e}")
        return None


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

    # Telegram rejects photos under ~100px on shortest side (PHOTO_INVALID_DIMENSIONS).
    # Pad small crops with the background color from the bottom-right pixel.
    MIN_DIM = 100
    cw, ch = cropped.size
    if cw < MIN_DIM or ch < MIN_DIM:
        new_w = max(cw, MIN_DIM)
        new_h = max(ch, MIN_DIM)
        bg = cropped.getpixel((cw - 1, ch - 1))
        padded = Image.new(cropped.mode, (new_w, new_h), bg)
        padded.paste(cropped, ((new_w - cw) // 2, (new_h - ch) // 2))
        cropped = padded

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


_CONTEXT_PCTS_MAX = 200

_context_pct_names = {}  # {pc_id: str} — chat names from .context_pcts

def _load_context_pcts():
    """Load per-chat context % and names from disk."""
    global _context_pct_names
    if not context_pcts_file.exists():
        return {}
    try:
        data = json.loads(context_pcts_file.read_text())
        _context_pct_names = {k: v['name'] for k, v in data.items() if isinstance(v, dict) and 'name' in v}
        return {k: v['pct'] for k, v in data.items() if isinstance(v, dict) and 'pct' in v}
    except Exception:
        return {}

def _save_context_pcts(pc_id=None, chat_name=None):
    """Persist per-chat context % to disk, pruning to most recent entries."""
    try:
        existing = {}
        if context_pcts_file.exists():
            existing = json.loads(context_pcts_file.read_text())
        for pid, pct in _context_pcts.items():
            entry = existing.get(pid, {})
            entry['pct'] = pct
            entry['ts'] = datetime.now().isoformat()
            if pid == pc_id and chat_name:
                entry['name'] = chat_name
            existing[pid] = entry
        if len(existing) > _CONTEXT_PCTS_MAX:
            sorted_entries = sorted(existing.items(), key=lambda x: x[1].get('ts', ''), reverse=True)
            existing = dict(sorted_entries[:_CONTEXT_PCTS_MAX])
        context_pcts_file.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass

if CONTEXT_MONITOR:
    _context_pcts = _load_context_pcts()
    if _context_pcts:
        print(f"[context-monitor] Restored {len(_context_pcts)} chat(s) from .context_pcts")
else:
    _context_pcts = {}


def cursor_prefill_input(text, conn=None):
    """Focus the input editor and insert text WITHOUT sending.
    Used to pre-fill the annotation so it rides with the user's next message.
    """
    global msg_id_counter
    c = conn or active_conn()
    with cdp_lock:
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(json.dumps({
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
        focus_result = json.loads(c.recv())
        focus_val = focus_result.get('result', {}).get('result', {}).get('value')
        if focus_val != 'OK':
            return focus_val

        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(json.dumps({
            'id': mid,
            'method': 'Input.insertText',
            'params': {'text': text + '\n'}
        }))
        json.loads(c.recv())
        return 'OK'


def cursor_clear_input(conn=None):
    """Focus the chat input editor, select all, and delete via execCommand."""
    global msg_id_counter
    c = conn or active_conn()
    with cdp_lock:
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(json.dumps({
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
                    if (!editor) return 'NO_EDITOR';
                    if (!editor.textContent.trim()) return 'EMPTY';
                    editor.focus();
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('delete');
                    return 'CLEARED';
                })();
            """, 'returnByValue': True}
        }))
        json.loads(c.recv())


def cursor_send_message(text, raw=False):
    """Focus the input editor, insert text, click send.
    Holds the CDP lock for the entire sequence to avoid monitor thread contention.
    Auto-prepends [Phone] [Day YYYY-MM-DD HH:MM] unless raw=True.
    """
    if not raw:
        timestamp = datetime.now().strftime('%a %Y-%m-%d %H:%M')
        text = f"[{timestamp}] [Phone] {text}"

    global msg_id_counter
    conn = active_conn()
    t0 = time.time()

    with cdp_lock:
        # 1. Focus editor
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(json.dumps({
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
                    // Move cursor to end so new text appends after any prefilled annotation
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    return 'OK';
                })();
            """, 'returnByValue': True}
        }))
        focus_result = json.loads(conn.recv())
        focus_val = focus_result.get('result', {}).get('result', {}).get('value')
        if focus_val != 'OK':
            return focus_val
        t1 = time.time()

        # 2. Insert text at end (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(json.dumps({
            'id': mid,
            'method': 'Input.insertText',
            'params': {'text': text}
        }))
        json.loads(conn.recv())
        t2 = time.time()

        # 3. Verify + click send (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(json.dumps({
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
        send_result = json.loads(conn.recv())
        result = send_result.get('result', {}).get('result', {}).get('value')

    t3 = time.time()
    print(f"[sender] Timing: focus={int((t1-t0)*1000)}ms insert={int((t2-t1)*1000)}ms verify+send={int((t3-t2)*1000)}ms total={int((t3-t0)*1000)}ms")
    return result


def cursor_new_chat():
    """Click the '+' button to create a new chat tab. Returns 'OK' or error."""
    return cdp_eval("""
        (function() {
            // Primary: the "New Chat" button in the auxiliary bar title
            const btn = document.querySelector('[data-command-id="auxiliaryBar.newAgentMenu"] a.codicon-add-two')
                     || document.querySelector('[data-command-id="composer.createNewComposerTab"] a.codicon-add-two')
                     || document.querySelector('a[aria-label*="New Chat"]');
            if (!btn) return 'ERROR: new-chat button not found';
            btn.click();
            return 'OK';
        })();
    """)


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


def cursor_get_turn_info(composer_prefix='', conn=None):
    """Get the last turn's user message and all AI response sections.
    
    Uses composer-human-ai-pair-container which groups one user message
    with all its AI responses as a single turn.
    Returns individual sections (not joined) for real-time streaming.
    'turn_id' = unique DOM id of the human message (detects new turns).
    'user_full' = complete user message for forwarding to Telegram.
    'images' = list of vscode-file:// image URLs attached to the message.
    
    If composer_prefix is given (e.g. 'b625b741' from pc_id 'cid-b625b741'),
    scopes the search to the content area with that data-composer-id.
    If conn is given, evaluates on that WebSocket instead of active_conn().
    """
    js = """
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

            const composerPrefix = '__COMPOSER_PREFIX__';
            let scope = document;
            if (composerPrefix) {
                const scoped = document.querySelector('[data-composer-id^="' + composerPrefix + '"]');
                if (!scoped) return JSON.stringify({ turn_id: '', user_full: '', sections: [], images: [], conv: '' });
                scope = scoped;
            }
            const containers = scope.querySelectorAll('.composer-human-ai-pair-container');
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
                    // Pending confirmation: find ALL action buttons in the status row
                    const statusRow = msg.querySelector('.composer-tool-call-status-row');
                    const actionBtns = statusRow ? statusRow.querySelectorAll('[data-click-ready="true"]') : [];

                    if (toolStatus === 'loading' && actionBtns.length > 0) {
                        // Collect all buttons universally (labels + indices)
                        const buttons = Array.from(actionBtns).map((btn, idx) => ({
                            label: btn.innerText.trim().replace(/\\s+/g, ' '),
                            index: idx
                        }));

                        const desc = msg.querySelector('.composer-tool-former-message');
                        // Extract text from specific DOM parts, ignoring control row (buttons)
                        // and Monaco diff editors (whose innerText changes async and breaks stability).
                        let cleanText = 'Action pending';
                        if (desc) {
                            const parts = [];
                            // File edit confirmation: filename + line stats + block status
                            const filename = desc.querySelector('.composer-code-block-filename');
                            if (filename) {
                                parts.push(filename.textContent.trim());
                                const fileStat = desc.querySelector('.composer-code-block-status');
                                if (fileStat) parts.push(fileStat.textContent.trim());
                                // Skip block-attribution-pill (Cursor's "Blocked" dropdown — not useful in Telegram)
                            }
                            // Tool call confirmation: headers + body
                            const topHeader = desc.querySelector('.composer-tool-call-top-header');
                            const header = desc.querySelector('.composer-tool-call-header');
                            const body = desc.querySelector('.composer-tool-call-body');
                            if (topHeader) parts.push(topHeader.innerText.trim().replace(/\\s+/g, ' '));
                            if (header) parts.push(header.innerText.trim().replace(/\\s+/g, ' '));
                            if (body && body.innerText.trim()) parts.push(body.innerText.trim());
                            if (!parts.length) {
                                // Fallback: clone desc, strip status row (buttons),
                                // walk text nodes and join with spaces (innerText
                                // doesn't insert spaces between flex items).
                                const clone = desc.cloneNode(true);
                                const sr = clone.querySelector('.composer-tool-call-status-row');
                                if (sr) sr.remove();
                                const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                                let node;
                                while (node = walker.nextNode()) {
                                    const t = node.textContent.trim();
                                    if (t) parts.push(t);
                                }
                            }
                            cleanText = parts.join(' ') || 'Action pending';
                        }
                        const bubbleSelector = '#bubble-' + bubbleSuffix;
                        sections.push({
                            text: cleanText,
                            type: 'confirmation',
                            id: toolCallId || ('gen:' + msgId + ':' + subIdx),
                            selector: bubbleSelector + ' .composer-tool-former-message > div',
                            buttons_selector: bubbleSelector + ' .composer-tool-call-status-row [data-click-ready="true"]',
                            buttons: buttons
                        });
                        return;
                    }

                    // File edit (code block with diff) — works for both
                    // auto-accepted (no buttons, stays loading) and completed edits.
                    // Stability mechanism handles the race condition: if buttons
                    // appear on the next tick, the section becomes a confirmation
                    // (different text/type) and stability resets.
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
                            selector: selector,
                            filename_selector: '#bubble-' + bubbleSuffix + ' .composer-code-block-filename',
                            file_stat: stat
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
                        const latexBlock = child.querySelector('.markdown-block-latex');
                        if (codeBlock) {
                            const text = child.innerText.trim();
                            // Use the section's unique DOM id for a reliable selector
                            // (:nth-of-type breaks because each code block is in a different parent section)
                            const selector = child.id
                                ? '#' + child.id + ' .markdown-block-code'
                                : '#bubble-' + bubbleSuffix + ' .markdown-block-code';
                            sections.push({
                                text: text,
                                type: 'code_block',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                        } else if (latexBlock) {
                            const text = child.innerText.trim();
                            const selector = child.id
                                ? '#' + child.id + ' .markdown-block-latex'
                                : '#bubble-' + bubbleSuffix + ' .markdown-block-latex';
                            sections.push({
                                text: text,
                                type: 'latex',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                        } else if (child.querySelector('.markdown-inline-latex')) {
                            const text = child.innerText.trim();
                            const selector = child.id
                                ? '#' + child.id
                                : '#bubble-' + bubbleSuffix;
                            sections.push({
                                text: text,
                                type: 'latex',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
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
    """.replace('__COMPOSER_PREFIX__', composer_prefix)
    result = cdp_eval_on(conn, js) if conn else cdp_eval(js)
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

                    if action == 'setup_commands':
                        if tool_id == 'yes':
                            ok = tg_register_commands()
                            tg_call('answerCallbackQuery', callback_query_id=cb_id,
                                    text='Commands registered!' if ok else 'Failed to register')
                            # Update the message to remove the buttons
                            cb_msg = callback.get('message', {})
                            if cb_msg:
                                tg_call('editMessageText', chat_id=cb_msg['chat']['id'],
                                        message_id=cb_msg['message_id'],
                                        text='✅ Command menu registered.')
                        else:
                            tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Skipped')
                            cb_msg = callback.get('message', {})
                            if cb_msg:
                                tg_call('editMessageText', chat_id=cb_msg['chat']['id'],
                                        message_id=cb_msg['message_id'],
                                        text='Command menu skipped. You can always add commands later via /setcommands in @BotFather.')
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
                                        cdp_bring_to_front(info['ws'], target_iid)
                                    except Exception as e:
                                        print(f"[sender] Could not bring window to front: {e}")
                                # Update mirrored_chat immediately (don't wait for overview thread)
                                chat_name = result if result and result != 'OK' else target_pc_id
                                mirrored_chat = (target_iid, target_pc_id, chat_name)
                                ws_label = (info.get('workspace') or '?').removesuffix(' (Workspace)')
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=f'Switched')
                                _save_active_chat(info.get('workspace'), chat_name, target_pc_id)
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
                    elif selectors and action.startswith('btn_'):
                        # Universal button click: action = "btn_INDEX"
                        try:
                            btn_index = int(action.split('_', 1)[1])
                        except (ValueError, IndexError):
                            tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Invalid button')
                            continue
                        btns_selector = selectors.get('buttons_selector', '')
                        btn_label = next((b['label'] for b in selectors.get('buttons', []) if b['index'] == btn_index), f'Button {btn_index}')
                        print(f"[sender] Callback: click button [{btn_index}] '{btn_label}' for tool {tool_id[:12]}...")
                        click_result = cdp_eval(f"""
                            (function() {{
                                const btns = document.querySelectorAll('{btns_selector}');
                                if (!btns[{btn_index}]) return 'ERROR: button ' + {btn_index} + ' not found (' + btns.length + ' buttons)';
                                btns[{btn_index}].click();
                                return 'OK';
                            }})();
                        """)
                        print(f"[sender] Click result: {click_result}")
                        tg_call('answerCallbackQuery', callback_query_id=cb_id, text=btn_label)
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
                    with chat_id_lock:
                        chat_id = cid
                    chat_id_file.write_text(str(cid))
                    print(f"[owner] Auto-paired with {user} (ID: {user_id})")
                    tg_send(cid, "🔗 You're in! Messages flow both ways now.\nUse /pause to mute, /play to resume.")
                    if tg_commands_need_update():
                        tg_ask_command_update(cid)
                    continue

                if status == 'rejected':
                    print(f"[sender] Rejected message from {user} (ID: {user_id})")
                    tg_send(cid, "Already paired with someone else.\nIf that's you on another device, send /unpair there first.\n\nWant your own? github.com/qmHecker/pocket-cursor")
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
                if text == '/start':
                    conv_name = cursor_get_active_conv()
                    status_line = "⏸ Paused" if muted else "▶ Active"
                    instances = len(instance_registry)
                    lines = [
                        f"PocketCursor is running. {status_line}",
                        f"{instances} workspace{'s' if instances != 1 else ''} connected.",
                    ]
                    if conv_name:
                        lines.append(f"💬 {conv_name}")
                    lines.append("\n/newchat /chats /pause /play /screenshot /unpair")
                    tg_send(cid, '\n'.join(lines))
                    continue

                if text == '/unpair':
                    OWNER_ID = None
                    if owner_file.exists():
                        owner_file.unlink()
                    tg_send(cid, "👋 Unpaired. Next message from anyone will pair them.")
                    print(f"[owner] Unpaired")
                    continue

                if text == '/pause':
                    muted = True
                    muted_file.touch()
                    tg_send(cid, "⏸ Paused. Nothing will be forwarded.\nSend /play when you're ready.")
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
                    print(f"[sender] Taking screenshot of {active_instance_id and active_instance_id[:8]}...")
                    try:
                        cdp_bring_to_front(active_conn(), active_instance_id)
                    except Exception:
                        pass
                    time.sleep(0.3)
                    png = cdp_screenshot()
                    if png:
                        tg_send_photo_bytes(cid, png, caption="Cursor IDE screenshot")
                        print(f"[sender] Screenshot sent ({len(png)} bytes)")
                    else:
                        tg_send(cid, "Failed to capture screenshot.")
                    continue

                if text == '/newchat':
                    print("[sender] Creating new chat...")
                    result = cursor_new_chat()
                    if not result or not result.startswith('OK'):
                        tg_send(cid, f"Failed: {result}")
                    print(f"[sender] New chat: {result}")
                    continue

                if text in ('/chats', '/agents', '/agent'):
                    grouped = {}
                    for iid, info in instance_registry.items():
                        convs = info.get('convs', {})
                        if not convs:
                            continue
                        ws_name = (info['workspace'] or '(no workspace)').removesuffix(' (Workspace)')
                        if ws_name not in grouped:
                            grouped[ws_name] = []
                        for pc_id, conv in convs.items():
                            is_mirrored = mirrored_chat and mirrored_chat[0] == iid and mirrored_chat[1] == pc_id
                            prefix = '▶ ' if is_mirrored else ''
                            grouped[ws_name].append([{'text': f"{prefix}{conv['name']}", 'callback_data': f"chat:{iid}:{pc_id}"}])
                    if grouped:
                        for ws_name, keyboard in grouped.items():
                            tg_call('sendMessage', chat_id=cid, text=f'📂 {ws_name}',
                                    reply_markup={'inline_keyboard': keyboard})
                    else:
                        tg_send(cid, "No open chats right now.")
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


def _composer_prefix_from_pcid(pc_id):
    """Extract composer-id prefix from a pc_id like 'cid-b625b741' → 'b625b741'."""
    if pc_id and pc_id.startswith('cid-'):
        return pc_id[4:]
    return ''


def monitor_thread():
    global mirrored_chat
    print("[monitor] Starting Cursor monitor...")
    last_turn_id = None         # Track conversation turns (DOM message id)
    last_conv = None            # Track active conversation name (for logging)
    mc = mirrored_chat          # Snapshot of mirrored_chat at init
    last_mc_pcid = mc[1] if mc else None   # Track by pc_id (stable across renames)
    last_iid = mc[0] if mc else active_instance_id
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

            # Get the last turn's info (scoped to mirrored chat's composer-id)
            # Use mirrored_chat's instance connection — not active_conn()
            mc = mirrored_chat
            cp = _composer_prefix_from_pcid(mc[1]) if mc else ''
            mc_conn = instance_registry.get(mc[0], {}).get('ws') if mc else None
            turn = cursor_get_turn_info(cp, conn=mc_conn)

            # Composer not in expected instance — search others (chat may have moved)
            if cp and not turn['turn_id']:
                found_iid = None
                for iid, info in instance_registry.items():
                    try:
                        turn = cursor_get_turn_info(cp, conn=info['ws'])
                        if turn['turn_id']:
                            found_iid = iid
                            break
                    except Exception:
                        pass

                if not found_iid:
                    continue

                ws_label = (instance_registry[found_iid].get('workspace') or found_iid[:8]).removesuffix(' (Workspace)')
                print(f"[monitor] Composer {cp} moved to {ws_label}, skipping {len(turn['sections'])} existing")
                mirrored_chat = (found_iid, mc[1], mc[2])
                last_iid = found_iid
                last_mc_pcid = mc[1]
                last_turn_id = turn['turn_id']
                last_conv = turn.get('conv', '')
                forwarded_ids = {
                    sec.get('id', '') for sec in turn['sections']
                    if isinstance(sec, dict) and sec.get('id')
                }
                prev_by_id = {sec.get('id', ''): sec.get('text', '')
                              for sec in turn['sections'] if isinstance(sec, dict) and sec.get('id')}
                section_stable = {}
                sent_this_turn = False
                marked_done = False
                continue

            turn_id = turn['turn_id']              # Unique DOM id per turn
            user_full = turn['user_full']           # Full text for forwarding
            sections = turn['sections']
            images = turn.get('images', [])
            conv = turn.get('conv', '')

            # Detect chat switch via mirrored_chat (set by chat_detection listener).
            # Uses pc_id which is stable across auto-renames — unlike conv name which
            # changes when AI renames the chat and caused false switch detections.
            mc = mirrored_chat
            cur_pcid = mc[1] if mc else None
            cur_iid = mc[0] if mc else active_instance_id
            switched = False
            if last_mc_pcid is not None and cur_pcid and cur_pcid != last_mc_pcid:
                switched = True
            if last_iid is not None and cur_iid and cur_iid != last_iid:
                switched = True

            if switched:
                if cur_iid != last_iid:
                    cp = _composer_prefix_from_pcid(cur_pcid) if cur_pcid else ''
                    turn = cursor_get_turn_info(cp)
                    if not turn['turn_id'] and not turn['sections']:
                        for _retry in range(8):
                            time.sleep(0.5)
                            turn = cursor_get_turn_info(cp)
                            if turn['turn_id'] or turn['sections']:
                                break
                    turn_id = turn['turn_id']
                    sections = turn['sections']
                    conv = turn.get('conv', '')
                cur_name = mc[2] if mc else conv
                prev_name = last_conv or f'instance {last_iid[:8] if last_iid else "?"}'
                print(f"[monitor] Switched: '{prev_name[:40]}' -> '{cur_name[:40]}', skipping {len(sections)} sections")
                forwarded_ids = {
                    sec.get('id', '') for sec in sections
                    if isinstance(sec, dict) and sec.get('id')
                }
                sent_this_turn = False
                prev_by_id = {sec.get('id', ''): sec.get('text', '')
                              for sec in sections if isinstance(sec, dict) and sec.get('id')}
                section_stable = {}
                marked_done = False
                if CONTEXT_MONITOR and cur_pcid:
                    ctx = get_context_pct(mc_conn)
                    if ctx is not None:
                        _context_pcts[cur_pcid] = ctx
                        _context_pct_names[cur_pcid] = cur_name
                        _save_context_pcts(pc_id=cur_pcid, chat_name=cur_name)
                        print(f"[context-monitor] Switch: {ctx}% in '{cur_name}'")
                last_turn_id = turn_id
                last_conv = conv
                last_mc_pcid = cur_pcid
                last_iid = cur_iid
                continue
            last_conv = conv
            last_mc_pcid = cur_pcid
            last_iid = cur_iid

            if turn_id != last_turn_id:
                if not initialized:
                    print(f"[monitor] Init: '{user_full[:50]}', skipping {len(sections)} existing")
                    if not muted and conv:
                        mc = mirrored_chat
                        ws_label = ''
                        if mc:
                            info = instance_registry.get(mc[0], {})
                            ws_label = (info.get('workspace') or '').removesuffix(' (Workspace)')
                        if ws_label:
                            tg_send(cid, f"\U0001f4ac Chat activated: {conv}  ({ws_label})")
                        else:
                            tg_send(cid, f"\U0001f4ac Chat activated: {conv}")
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

                if not user_full:
                    print(f"[monitor] user_full empty, polling (turn_id={short_id(turn_id)})...")
                    for attempt in range(10):
                        time.sleep(0.2)
                        t = cursor_get_turn_info(cp)
                        t_tid = t['turn_id']
                        t_uf = t['user_full']
                        if t_tid != turn_id:
                            print(f"[monitor]   poll {attempt}: turn_id changed -> {short_id(t_tid)}, abort")
                            break
                        if t_uf:
                            print(f"[monitor]   poll {attempt}: got '{t_uf[:40]}'")
                            user_full = t_uf
                            sections = t['sections'] or sections
                            images = t.get('images') or images
                            break
                    else:
                        print(f"[monitor]   poll exhausted, user_full still empty")

                # Check if this came from Telegram or was typed directly in Cursor
                with last_sent_lock:
                    sent = last_sent_text
                from_telegram = (sent and (
                    sent[:30] in user_full
                    or sent == '[photo]'
                ))

                origin = "Telegram" if from_telegram else "Cursor"
                print(f"[monitor] New turn ({origin}): '{user_full[:50]}'")
                for idx, sec in enumerate(sections):
                    if isinstance(sec, dict):
                        print(f"  [{idx}] {sec.get('type', '?'):12s}  id={short_id(sec.get('id'))}")

                if CONTEXT_MONITOR and mirrored_chat:
                    cur_pcid = mirrored_chat[1]
                    prev_pct = _context_pcts.get(cur_pcid)
                    ctx = get_context_pct(mc_conn)
                    ann = _build_context_annotation(ctx, cur_pcid)
                    if ctx is not None:
                        _context_pcts[cur_pcid] = ctx
                        chat_label = mirrored_chat[2] if mirrored_chat else cur_pcid
                        _context_pct_names[cur_pcid] = chat_label
                        _save_context_pcts(pc_id=cur_pcid, chat_name=chat_label)
                        lines = [f"[context-monitor] {ctx}% used in '{chat_label}'"]
                        for pid, pct in _context_pcts.items():
                            name = _context_pct_names.get(pid, pid)
                            if pid == cur_pcid:
                                delta = ctx - prev_pct if prev_pct is not None else 0
                                trend = " 📈" if delta > 0 else " 📉" if delta < 0 else ""
                                lines.append(f"  {name}: {pct:.1f}%{trend}")
                            else:
                                lines.append(f"  {name}: {pct:.1f}%")
                        print('\n'.join(lines))
                    if ann:
                        try:
                            cursor_prefill_input(ann, conn=mc_conn)
                            print(f"[context-monitor] Prefilled: {ann}")
                        except Exception as e:
                            print(f"[context-monitor] Failed to prefill: {e}")

                if not from_telegram:
                    if not muted and user_full:
                        tg_send(cid, f"[PC] {user_full}")

                        for img_url in images:
                            local_path = vscode_url_to_path(img_url)
                            if local_path and Path(local_path).exists():
                                print(f"[monitor] Forwarding image: {Path(local_path).name}")
                                tg_send_photo(cid, local_path, caption="[PC] attached image")

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

            # [SILENT] scan: if ANY section contains [SILENT], suppress entire response
            turn_silent = any(
                '[SILENT]' in (s['text'] if isinstance(s, dict) else s)
                for s in sections
            )
            if turn_silent and not getattr(monitor_thread, '_silent_logged', False):
                print(f"[monitor] [SILENT] detected — suppressing entire response")
                for s in sections:
                    sk = s.get('id', '') if isinstance(s, dict) else ''
                    if sk:
                        forwarded_ids.add(sk)
                sent_this_turn = True
                monitor_thread._silent_logged = True
            if not turn_silent:
                monitor_thread._silent_logged = False

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
                    buttons = sec.get('buttons', [])
                    btns_selector = sec.get('buttons_selector', '')
                    with pending_confirms_lock:
                        pending_confirms[tool_id] = {
                            'buttons_selector': btns_selector,
                            'buttons': buttons
                        }
                    if not muted:
                        tg_typing(cid)
                        png = None
                        if sec_selector:
                            png = cdp_screenshot_element(sec_selector)
                        keyboard = []
                        for btn in buttons:
                            keyboard.append([{
                                'text': btn['label'],
                                'callback_data': f"btn_{btn['index']}:{tool_id}"
                            }])
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
                    if sec_type in ('table', 'file_edit', 'code_block', 'latex'):
                        file_path = None
                        if sec_type == 'file_edit':
                            fn_sel = sec.get('filename_selector') if isinstance(sec, dict) else None
                            if fn_sel:
                                file_path = cdp_hover_file_path(fn_sel)
                                if file_path:
                                    print(f"[monitor] File path: {file_path}")
                        png = None
                        if sec_selector:
                            png = cdp_screenshot_element(sec_selector)
                        if not png and sec_type == 'table':
                            png = cdp_screenshot_element(
                                '.composer-human-ai-pair-container:last-child [data-message-role="ai"] .markdown-table-container'
                            )
                        label = {'table': 'TABLE', 'file_edit': 'FILE_EDIT', 'code_block': 'CODE_BLOCK', 'latex': 'LATEX'}[sec_type]
                        if sec_type == 'file_edit':
                            stat = sec.get('file_stat', '') if isinstance(sec, dict) else ''
                            display = file_path or text
                            if file_path and stat:
                                display = f"{file_path} {stat}"
                            caption = f"📝 {display}"
                        else:
                            caption = ''
                        if png:
                            print(f"[monitor] Forwarding section {i+1} as {label} screenshot ({len(png)} bytes)")
                            tg_send_photo_bytes(cid, png, filename=f'{sec_type}.png', caption=caption)
                        else:
                            print(f"[monitor] {label} screenshot failed, sending as text ({len(text)} chars)")
                            prefix = '📝 ' if sec_type == 'file_edit' else ''
                            display_text = (file_path or text) if sec_type == 'file_edit' else text
                            tg_send(cid, f"{prefix}{display_text}")
                    elif sec_type == 'thinking':
                        print(f"[monitor] Forwarding THINKING ({len(text)} chars)")
                        tg_send_thinking(cid, text)
                    else:
                        # Check for [PHONE_OUTBOX:filename] marker
                        outbox_match = OUTBOX_MARKER_RE.search(text)
                        if outbox_match:
                            outbox_filename = outbox_match.group(1).strip()
                            caption = OUTBOX_MARKER_RE.sub('', text).strip()
                            # Wait up to 15s for the file to appear
                            outbox_file = phone_outbox / outbox_filename
                            print(f"[monitor] Outbox marker: waiting for {outbox_filename}")
                            deadline = time.time() + 15
                            while not outbox_file.exists() and time.time() < deadline:
                                time.sleep(1)
                            if outbox_file.exists():
                                outbox_render_and_send(outbox_filename, cid, caption=caption)
                            else:
                                print(f"[monitor] Outbox file not found after 15s: {outbox_filename}")
                                if caption:
                                    tg_send(cid, caption)
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
# Active chat detection is event-driven via chat_detection.py:
# click/focusin events → JS detectChat() → __pc_report binding → Python callback.
# See docs/active-chat-detection-plan.md for full design.
#
# This thread handles instance lifecycle (new/closed/workspace changes)
# and periodic conversation scans (new/closed/renamed chats).

SCAN_INTERVAL = 3     # seconds between full rescans
SCAN_VERBOSE = False  # True = log every chat per scan (fingerprint details)
CONTEXT_MONITOR_THRESHOLD = 85

def overview_thread():
    """Periodically rescan CDP targets. Detect new/closed Cursor instances."""
    global ws, active_instance_id, mirrored_chat
    print("[overview] Starting instance monitor...")
    if not mirrored_chat and active_instance_id and active_instance_id in instance_registry:
        info = instance_registry[active_instance_id]
        for pc_id, conv in info.get('convs', {}).items():
            if conv['active']:
                mirrored_chat = (active_instance_id, pc_id, conv['name'])
                break
    while True:
        try:
            time.sleep(SCAN_INTERVAL)

            port = detect_cdp_port()
            current = cdp_list_instances(port)
            current_ids = {inst['id'] for inst in current}
            known_ids = set(instance_registry.keys())

            for inst in current:
                if inst['id'] not in known_ids:
                    label = inst['workspace'] or '(no workspace)'
                    is_detach = inst['workspace'] and any(
                        info['workspace'] == inst['workspace'] for info in instance_registry.values()
                    )
                    try:
                        conn = websocket.create_connection(inst['ws_url'])
                        listener_conn = _setup_chat_listener(inst['id'], inst['ws_url'], label)
                        with cdp_lock:
                            instance_registry[inst['id']] = {
                                'workspace': inst['workspace'],
                                'title': inst['title'],
                                'ws': conn,
                                'ws_url': inst['ws_url'],
                                'listener_ws': listener_conn,
                                'convs': {},
                            }
                        if is_detach:
                            print(f"[overview] Detached window: {label}  [{inst['id'][:8]}]")
                        else:
                            print(f"[overview] Opened: {label}  [{inst['id'][:8]}]")
                            if chat_id and not muted and inst['workspace']:
                                tg_send(chat_id, f"📂 Workspace opened: {label}")
                    except Exception as e:
                        print(f"[overview] Failed to connect to {label}: {e}")

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
                if info:
                    label = info['workspace'] or '(no workspace)'
                    try:
                        info['ws'].close()
                    except Exception:
                        pass
                    try:
                        info.get('listener_ws', None) and info['listener_ws'].close()
                    except Exception:
                        pass
                    is_merge = info['workspace'] and any(
                        v['workspace'] == info['workspace'] for v in instance_registry.values()
                    )
                    if is_merge:
                        print(f"[overview] Window merged: {label}  [{iid[:8]}]")
                    else:
                        print(f"[overview] Closed: {label}  [{iid[:8]}]")
                        if chat_id and not muted:
                            tg_send(chat_id, f"📂 Workspace closed: {label}")
                    if is_active and active_instance_id:
                        new_name = instance_registry[active_instance_id]['workspace'] or '(no workspace)'
                        print(f"[overview] Active switched to: {new_name}")
                        if chat_id and not muted and not is_merge:
                            tg_send(chat_id, f"📂 Workspace activated: {new_name}")

            # Detect workspace changes (e.g. user picked a workspace in empty instance)
            for inst in current:
                if inst['id'] in instance_registry:
                    old = instance_registry[inst['id']]
                    if old['workspace'] != inst['workspace'] and inst['workspace']:
                        with cdp_lock:
                            old['workspace'] = inst['workspace']
                            old['title'] = inst['title']
                        print(f"[overview] Workspace opened: {inst['workspace']}  [{inst['id'][:8]}]")
                        if chat_id and not muted:
                            tg_send(chat_id, f"📂 Workspace opened: {inst['workspace']}")

            # Reconnect dead listeners
            for iid, info in list(instance_registry.items()):
                if info.get('listener_dead'):
                    label = info.get('workspace') or '(no workspace)'
                    try:
                        old_ws = info.get('listener_ws')
                        if old_ws:
                            try:
                                old_ws.close()
                            except Exception:
                                pass
                        listener_conn = _setup_chat_listener(iid, info['ws_url'], label)
                        info['listener_ws'] = listener_conn
                        info.pop('listener_dead', None)
                        print(f"[overview] Listener reconnected: {label}")
                    except Exception as e:
                        print(f"[overview] Listener reconnect failed for {label}: {e}")

            # Scan conversations per instance. Each CDP target gets its own scan
            # because detached windows have distinct DOMs despite sharing a workspace name.
            scan_summary = {}
            for iid, info in list(instance_registry.items()):
                ws_name = info['workspace']
                if not ws_name:
                    continue
                try:
                    convs = list_chats(lambda js, c=info['ws']: cdp_eval_on(c, js))
                except Exception:
                    continue

                current_convs = {c['pc_id']: c for c in convs}
                known_convs = info['convs']
                ws_label = ws_name.removesuffix(' (Workspace)')
                if SCAN_VERBOSE:
                    for c in convs:
                        mid = c.get('msg_id', '-')
                        mid_short = mid[:12] if mid and mid != '-' else '-'
                        print(f"[overview] fingerprint: {c['pc_id']}  msg={mid_short:14s}  \"{c['name']}\"  in {ws_label}")

                # Fingerprint-scoring: match disappeared↔appeared entries
                disappeared = set(known_convs) - set(current_convs)
                appeared = set(current_convs) - set(known_convs)

                if disappeared or appeared:
                    d_names = {pid: known_convs[pid]['name'] for pid in disappeared}
                    a_names = {pid: current_convs[pid]['name'] for pid in appeared}
                    print(f"[overview] diff: disappeared={d_names}  appeared={a_names}  in {ws_label}")

                if disappeared and appeared:
                    scores = {}  # (appeared_id, disappeared_id) → score
                    for a_id in appeared:
                        a = current_convs[a_id]
                        for d_id in disappeared:
                            d = known_convs[d_id]
                            score = 0
                            a_mid = a.get('msg_id')
                            d_mid = d.get('msg_id')
                            if a_mid and d_mid and a_mid == d_mid:
                                score += 3
                            if a.get('name') == d.get('name'):
                                score += 1
                            if score > 0:
                                scores[(a_id, d_id)] = score

                    if scores:
                        print(f"[overview] scores: {scores}  in {ws_label}")
                    else:
                        print(f"[overview] scores: EMPTY (no matches found)  in {ws_label}")

                    matched_a = set()
                    matched_d = set()
                    for (a_id, d_id), score in sorted(scores.items(), key=lambda x: -x[1]):
                        if a_id in matched_a or d_id in matched_d:
                            continue
                        # Check for ambiguity: is there another pair with the same score for this d_id?
                        rivals = [s for (ai, di), s in scores.items() if di == d_id and ai != a_id and s == score]
                        if rivals:
                            print(f"[overview] Ambiguous match for \"{known_convs[d_id]['name']}\" (score={score}, {len(rivals)+1} candidates) — skipping  in {ws_label}")
                            continue
                        mid_info = f"msg={current_convs[a_id].get('msg_id', '-')[:12]}" if current_convs[a_id].get('msg_id') else "msg=-"
                        print(f"[overview] Linked: {d_id} -> {a_id}  score={score}  {mid_info}  \"{known_convs[d_id]['name']}\"  in {ws_label}")
                        known_convs[a_id] = known_convs.pop(d_id)
                        matched_a.add(a_id)
                        matched_d.add(d_id)

                    disappeared -= matched_d
                    appeared -= matched_a

                for pc_id in appeared:
                    print(f"[overview] New conversation: {current_convs[pc_id]['name']}  in {ws_label}")

                for pc_id in disappeared:
                    print(f"[overview] Conversation closed: {known_convs[pc_id]['name']}  in {ws_label}")

                for pc_id, conv in current_convs.items():
                    if pc_id in known_convs and known_convs[pc_id]['name'] != conv['name']:
                        old_name = known_convs[pc_id]['name']
                        print(f"[overview] Conversation renamed: {old_name} → {conv['name']}  in {ws_label}")
                        if chat_id and not muted:
                            tg_send(chat_id, f"💬 Chat renamed: {old_name} → {conv['name']}  ({ws_label})")

                info['convs'] = {pc_id: {'name': c['name'], 'active': c['active'], 'msg_id': c.get('msg_id')} for pc_id, c in current_convs.items()}
                scan_summary[ws_label] = scan_summary.get(ws_label, 0) + len(convs)

            if scan_summary:
                parts = '  '.join(f"{ws} ({n})" for ws, n in scan_summary.items())
                print(f"[overview] chat scan: {parts}")

        except Exception as e:
            print(f"[overview] Error: {e}")
            time.sleep(5)


# ── Phone outbox renderer ─────────────────────────────────────────────────────

_render_local = os.environ.get('RENDER_LOCAL_DIR', '').strip()
if _render_local:
    MD_TO_IMAGE_SCRIPT = Path(_render_local) / 'md_to_image.mjs'
    if MD_TO_IMAGE_SCRIPT.exists():
        print(f"[outbox] Using local render: {MD_TO_IMAGE_SCRIPT}")
    else:
        print(f"[outbox] WARNING: RENDER_LOCAL_DIR set but {MD_TO_IMAGE_SCRIPT} not found. Run setup_local_render.py")
        MD_TO_IMAGE_SCRIPT = Path(__file__).parent / 'md_to_image.mjs'
else:
    MD_TO_IMAGE_SCRIPT = Path(__file__).parent / 'md_to_image.mjs'
OUTBOX_MARKER_RE = re.compile(r'\[PHONE_OUTBOX:([^\]]+)\]')
phone_outbox.mkdir(exist_ok=True)


def outbox_render_and_send(filename, cid, caption=None):
    """Render an outbox file and send it to Telegram. Returns True on success.
    
    Width convention: 'name.w800.md' → render at 800px. Default 450px.
    """
    f = phone_outbox / filename
    if not f.is_file():
        return False

    ext = f.suffix.lower()
    png_bytes = None

    if ext == '.md':
        # Parse optional width from filename: name.w800.md → 800
        width_match = re.search(r'\.w(\d+)\.md$', f.name, re.IGNORECASE)
        width_args = ['--width', width_match.group(1)] if width_match else []

        png_path = f.with_suffix('.png')
        try:
            result = sp.run(
                ['node', str(MD_TO_IMAGE_SCRIPT), str(f), '--out', str(png_path)] + width_args,
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=60
            )
            if result.returncode != 0:
                print(f"[outbox] Render failed: {result.stderr.strip()}")
                return False
            png_bytes = png_path.read_bytes()
        except Exception as e:
            print(f"[outbox] Render error: {e}")
            return False
        finally:
            try:
                f.unlink(missing_ok=True)
                png_path.unlink(missing_ok=True)
            except Exception:
                pass

    elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        try:
            png_bytes = f.read_bytes()
            f.unlink()
        except Exception as e:
            print(f"[outbox] Read error: {e}")
            return False

    if png_bytes:
        tg_send_photo_bytes(cid, png_bytes, filename=f'{f.stem}.png', caption=caption)
        print(f"[outbox] Sent {filename} ({len(png_bytes)} bytes)" + (f" with caption ({len(caption)} chars)" if caption else ""))
        return True
    return False

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

# Set bot description if not already configured (shown to new users above the START button)
_desc = tg_call('getMyDescription')
if not _desc.get('result', {}).get('description'):
    tg_call('setMyDescription',
            description="Your Cursor IDE, in your pocket.\n\nTap START to pair. Your conversations then flow both ways between Cursor and Telegram.")
_short = tg_call('getMyShortDescription')
if not _short.get('result', {}).get('short_description'):
    tg_call('setMyShortDescription',
            short_description="Cursor IDE ↔ Telegram bridge")

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

# Check if bot commands need updating and ask the user
if chat_id and tg_commands_need_update():
    print("[telegram] Command menu is outdated, asking user to update...")
    tg_ask_command_update(chat_id)

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
