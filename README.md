# PocketCursor

**Your Cursor IDE, in your pocket.**

The conversation doesn't have to end. PocketCursor bridges your running Cursor IDE to Telegram, so you can keep collaborating with your AI assistant from your phone — same conversation, same files, same context.

Not a remote agent. Not a cloud service. YOUR Cursor, accessible from wherever you are.

[![Demo video](https://img.youtube.com/vi/hK7GIbRTzYo/maxresdefault.jpg)](https://www.youtube.com/watch?v=hK7GIbRTzYo)

## Features

- **Full bidirectional mirroring** — messages flow both ways between Cursor and Telegram, regardless of where you type
- **Voice messages** — speak into Telegram, get transcription via OpenAI, forwarded to Cursor as text
- **Section-by-section streaming** — AI responses stream to your phone as they're generated, not as one block when done
- **Code block screenshots** — syntax-highlighted code arrives as clean screenshots, readable on a phone screen
- **Table & file edit screenshots** — rich content that doesn't translate to plain text gets screenshotted automatically
- **Interactive confirmations** — Accept/Reject prompts forwarded as Telegram inline buttons, tap to respond
- **Image paste** — send photos from your phone, they get pasted into Cursor's editor
- **`/screenshot`** — capture your entire Cursor window and view it on your phone
- **`/pause` & `/play`** — mute forwarding when you're at your desk, resume when you leave
- **`/chats`** — view and switch chats across all Cursor instances, with window activation
- **Multi-instance awareness** — detects all open Cursor windows, notifies on open/close
- **Chat notifications** — new chats, closed chats, renames, and active chat changes sent to Telegram
- **Thinking sections** — AI reasoning forwarded in italic with 💭 prefix
- **Auto-follows your focus** — switch chats by clicking tabs, typing in an input, or using `/chats` from your phone — the bridge follows automatically
- **Owner lock** — auto-pairs with the first user, rejects everyone else
- **Phone outbox** — the AI writes a `.md` file to `_phone_outbox/`, references it with a `[PHONE_OUTBOX:file.md]` marker, and it arrives on your phone as a styled image with optional caption
- **Single-instance guard** — prevents duplicate bridge processes via PID lock file
- **Works through lock screen** — lock your PC, walk away, keep working from your phone

## How It Works

PocketCursor uses the Chrome DevTools Protocol (CDP) to talk to Cursor. Cursor is an Electron app (Chromium under the hood), so launching it with a debug port gives full DOM access via WebSocket.

A three-thread Python script does the rest:
- **Sender thread** — polls Telegram for messages, injects them into Cursor's Lexical editor via CDP
- **Monitor thread** — watches Cursor's DOM for new AI responses, streams them section-by-section to Telegram
- **Overview thread** — monitors all Cursor instances and chats, sends notifications on changes

```
Phone (Telegram) ←→ pocket_cursor.py ←→ Cursor IDE (CDP) × N instances
```

Everything runs locally on your PC. No server, no cloud, no third-party services (except Telegram's free bot API and optionally OpenAI for voice transcription).

## Setup

### 1. Launch Cursor with CDP enabled

The easiest way — auto-finds Cursor and launches it with the right flags:

```bash
python start_cursor.py
```

Or launch manually with these flags:

```
cursor --remote-debugging-port=9222 --remote-allow-origins=http://localhost:9222
```

> **Tip:** Edit your desktop shortcut to always include these flags so you don't have to remember them.

### 2. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts, copy the bot token
3. Register the command menu so commands appear in Telegram's UI:

```
/setcommands
```

Then send:

```
pause - Mute Cursor → Telegram forwarding
play - Resume forwarding
screenshot - Capture Cursor window
chats - View and switch chats across all Cursor instances
unpair - Reset owner lock
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your bot token (and optionally your OpenAI API key for voice):

```
TELEGRAM_BOT_TOKEN=your-token-here
OPENAI_API_KEY=your-key-here
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
npm install
```

### 5. Run

```bash
python -X utf8 pocket_cursor.py
```

Send any message to your bot on Telegram — it auto-pairs with the first user.

To restart without losing phone access (useful when Cursor asks for approval):

```bash
python restart_pocket_cursor.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/pause` | Mute Cursor → Telegram forwarding (monitor keeps tracking internally) |
| `/play` | Resume forwarding |
| `/screenshot` | Capture and send the Cursor window |
| `/chats` | View and switch chats across all Cursor instances (inline buttons) |
| `/unpair` | Reset owner lock for re-pairing |

## Cursor Rule (optional but recommended)

A [Cursor rule](https://docs.cursor.sh/context/rules-for-ai) is included so the AI knows how to restart the bridge and use the phone outbox without being told every time.

Copy `pocket-cursor.mdc` into your workspace's `.cursor/rules/` folder and replace `<POCKET_CURSOR_DIR>` with the actual path to your PocketCursor installation:

```bash
# Example (adjust paths to your setup)
mkdir -p .cursor/rules
cp /path/to/pocket-cursor/pocket-cursor.mdc .cursor/rules/
```

Then open the copied file and replace all `<POCKET_CURSOR_DIR>` placeholders with the real path, e.g. `C:\Tools\pocket-cursor` or `/home/user/pocket-cursor`.

## Requirements

- **Python 3.10+**
- **Node.js 18+** (for markdown-to-image rendering via Puppeteer)
- **Cursor IDE** launched with `--remote-debugging-port=9222`
- **Telegram bot token** (free, via @BotFather)
- **OpenAI API key** (optional, for voice message transcription)

## License

MIT — see [LICENSE](LICENSE).
