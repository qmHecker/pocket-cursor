# PocketCursor

**Your Cursor, from your phone. The conversation doesn't have to end.**

To some people Cursor is "just" an IDE. To others, it's a coworker, a sparring partner, someone you actually think with. When you step away from your computer, those conversations end.

PocketCursor keeps them going. It connects your running Cursor to Telegram on your phone. Everything mirrors both ways. Not a remote agent. Not a cloud service. YOUR Cursor, wherever you are.

[▶ Watch the demo video](https://www.youtube.com/watch?v=hK7GIbRTzYo)

## What it's like to use

- **Voice messages.** Speak into Telegram. It gets transcribed and forwarded to Cursor.
- **Streaming responses.** AI responses arrive on your phone section by section, as they're generated. Not one block when done.
- **Code as screenshots.** Syntax-highlighted code blocks arrive as clean, readable images on your phone.
- **Interactive confirmations.** When Cursor needs approval, buttons appear on Telegram. Tap to accept or reject.
- **Images both ways.** Send photos from your phone into Cursor's editor. Images from Cursor arrive on Telegram.
- **Multi-workspace.** Detects all open Cursor workspaces. Switch between them from your phone via `/chats`.
- **Auto-follows your focus.** Switch chats on your PC and the bridge follows. Switch from your phone and Cursor follows. Both directions, always in sync.
- **Works through lock screen.** Lock your PC, walk away, keep going.

### Commands

| Command | Description |
|---------|-------------|
| `/newchat` | Start a fresh conversation |
| `/chats` | Show your open chats across all workspaces (tap to switch) |
| `/pause` | Mute forwarding |
| `/play` | Resume forwarding |
| `/screenshot` | Screenshot your Cursor window |
| `/unpair` | Disconnect this device |
| `/start` | Show status and commands |

### Under the hood

Also included: tables and file edit diffs as auto-screenshots, AI thinking sections (💭 prefix), chat notifications (new/closed/renamed chats), phone outbox (AI sends styled `.md` files as images), owner lock (auto-pairs with first user), single-instance guard (prevents duplicate processes).

## How it works

PocketCursor uses the Chrome DevTools Protocol (CDP) to talk to Cursor. Cursor is an Electron app (Chromium under the hood), so launching it with a debug port gives full DOM access via WebSocket.

A three-thread Python script does the rest:
- **Overview thread** monitors all Cursor instances and chats, sends notifications on changes
- **Sender thread** polls Telegram for messages, injects them into Cursor's Lexical editor via CDP
- **Monitor thread** watches Cursor's DOM for new AI responses, streams them to Telegram

```
Phone (Telegram) ←→ pocket_cursor.py ←→ all open Cursor windows
```

Everything runs locally on your machine. No server, no cloud, no middlemen. Just Telegram's free bot API and optionally OpenAI for voice transcription.

## Setup

### 1. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts, copy the bot token

PocketCursor sets up the bot description and command menu automatically on first pair.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your bot token (and optionally your OpenAI API key for voice):

```
TELEGRAM_BOT_TOKEN=your-token-here
OPENAI_API_KEY=your-key-here
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
npm install
```

### 4. Launch Cursor with CDP enabled

Auto-finds Cursor and launches with the right flags:

```bash
python start_cursor.py
```

Or launch manually:

```
cursor --remote-debugging-port=9222 --remote-allow-origins=http://localhost:9222
```

> **Tip:** Edit your desktop shortcut to always include these flags so you don't have to remember them.

### 5. Run

```bash
python -X utf8 pocket_cursor.py
```

Send any message to your bot on Telegram. It auto-pairs with the first user.

If Cursor requires you to confirm command execution, a normal restart (kill + start) would lock you out. The bridge is offline between the two confirmations and you can't confirm the second one. This script kills and restarts in a single command, so you only confirm once:

```bash
python restart_pocket_cursor.py
```

## Context Monitor (optional)

PocketCursor can monitor your context window fill level and remind the AI to save its working memory before a summary compresses it.

When enabled, it prefills an annotation like `[ContextMonitor: 85% context used -- journal reminder]` directly into the chat input field after each AI response. The annotation rides with the user's next message — no extra LLM roundtrip, no separate turn. The AI decides what to do with the reminder — it might write a journal entry at `.pocket-cursor/journals/`, or it might not. The journal is the AI's personal memory space.

To enable, add to your `.env`:

```
CONTEXT_MONITOR=true
```

You also need the context monitor section in your Cursor rule (included in `pocket-cursor.mdc`) so the AI knows how to interpret the annotations.

## Cursor Rule (optional but recommended)

A [Cursor rule](https://cursor.com/docs/context/rules) is included so the AI knows how to restart the bridge and use the phone outbox without being told every time.

Copy `pocket-cursor.mdc` into your workspace's `.cursor/rules/` folder and replace `<POCKET_CURSOR_DIR>` with the actual path to your PocketCursor installation:

```bash
mkdir -p .cursor/rules
cp /path/to/pocket-cursor/pocket-cursor.mdc .cursor/rules/
```

Then open the copied file and replace all `<POCKET_CURSOR_DIR>` placeholders with the real path.

## Requirements

- **Python 3.10+**
- **Node.js 18+** (for markdown-to-image rendering via Puppeteer)
- **Cursor IDE** launched with `--remote-debugging-port=9222`
- **Telegram bot token** (free, via @BotFather)
- **OpenAI API key** (optional, for voice transcription)

## License

MIT. Build whatever you want with it.
