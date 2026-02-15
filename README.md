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
- **`/agents`** — switch between conversation tabs remotely
- **Thinking sections** — AI reasoning forwarded in italic with 💭 prefix
- **Conversation awareness** — detects tab switches, shows which conversation is active
- **Owner lock** — auto-pairs with the first user, rejects everyone else
- **Works through lock screen** — lock your PC, walk away, keep working from your phone

## How It Works

PocketCursor uses the Chrome DevTools Protocol (CDP) to talk to Cursor. Cursor is an Electron app (Chromium under the hood), so launching it with a debug port gives full DOM access via WebSocket.

A two-thread Python script does the rest:
- **Sender thread** — polls Telegram for messages, injects them into Cursor's Lexical editor via CDP
- **Monitor thread** — watches Cursor's DOM for new AI responses, streams them section-by-section to Telegram

```
Phone (Telegram) ←→ pocket_cursor.py ←→ Cursor IDE (CDP)
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
agents - List and switch conversation tabs
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
```

### 5. Run

```bash
python -X utf8 pocket_cursor.py
```

Send any message to your bot on Telegram — it auto-pairs with the first user.

## Commands

| Command | Description |
|---------|-------------|
| `/pause` | Mute Cursor → Telegram forwarding (monitor keeps tracking internally) |
| `/play` | Resume forwarding |
| `/screenshot` | Capture and send the Cursor window |
| `/agents` | Show all conversation tabs as clickable buttons |
| `/agent <name>` | Switch to a conversation by name (substring match) |
| `/unpair` | Reset owner lock for re-pairing |

## Requirements

- **Python 3.10+**
- **Cursor IDE** launched with `--remote-debugging-port=9222`
- **Telegram bot token** (free, via @BotFather)
- **OpenAI API key** (optional, for voice message transcription)

## License

MIT — see [LICENSE](LICENSE).
