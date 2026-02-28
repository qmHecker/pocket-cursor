# PocketCursor

**Your Cursor, from your phone. The conversation doesn't have to end.**

To some people Cursor is "just" an IDE. To others, it's a coworker, a sparring partner, someone you actually think with. When you step away from your computer, those conversations end.

PocketCursor keeps them going. It connects your running Cursor to Telegram on your phone. Everything mirrors both ways. Not a remote agent. Not a cloud service. YOUR Cursor, wherever you are.

[▶ Watch the demo video](https://www.youtube.com/watch?v=hK7GIbRTzYo)

## What it's like to use

- **Voice messages.** Speak into Telegram. It gets transcribed and forwarded to Cursor.
- **Voice replies.** The AI can reply as a voice message. Text-to-speech via ElevenLabs, delivered as a Telegram voice note.
- **Streaming responses.** AI responses arrive on your phone section by section, as they're generated. Not one block when done.
- **Rich content as images.** Code blocks, LaTeX formulas, tables, and file diffs arrive as clean screenshots on your phone.
- **Interactive confirmations.** When Cursor needs approval, the action buttons appear on Telegram. One tap.
- **Command rules.** Safe commands like `git status` can auto-run without waiting for your phone. You define allow and deny patterns. Deny always wins.
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

Also included: AI thinking sections (💭 prefix), chat notifications (new/closed/renamed chats), phone outbox (AI sends styled `.md` files as images), silent mode (AI can suppress forwarding for housekeeping), owner lock (auto-pairs with first user), single-instance guard (prevents duplicate processes).

## How it works

PocketCursor uses the Chrome DevTools Protocol (CDP) to talk to Cursor. Cursor is an Electron app (Chromium under the hood), so launching it with a debug port gives full DOM access via WebSocket.

A Python script with three main threads does the rest:
- **Sender thread** polls Telegram for messages, injects them into Cursor's Lexical editor via CDP
- **Monitor thread** watches Cursor's DOM for new AI responses, streams them to Telegram
- **Overview thread** tracks Cursor instances and conversations (open, close, rename)

Each Cursor window also gets a lightweight event listener that detects chat switches instantly via DOM events — no polling.

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

Edit `.env` and add your Telegram bot token. The file lists all available options with descriptions.

```
TELEGRAM_BOT_TOKEN=your-token-here
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

## Command Rules (optional)

When Cursor asks for confirmation before running a command, the buttons appear on Telegram for you to tap. That works well for important decisions, but gets tedious for harmless things like `ls`, `git status`, or `cd`. Command rules let you define which commands auto-run and which always require your approval.

Cursor has a built-in allowlist, but it works at executable level: allowing `Shell(python)` means trusting *every* Python script. Command rules match the full command text, so you can allow `python *restart_pocket_cursor.py*` without blindly trusting all of Python. Deny patterns scan the entire command string, including chained commands, so `ls && rm -rf /` is blocked even though `ls` alone would pass.

Every auto-accepted command still sends a screenshot notification to Telegram, so you stay informed even when away from your desk.

```json
{
  "allow": [
    { "group": "Shell (safe)", "patterns": ["ls *", "cd *", "git status*", "git diff*"] }
  ],
  "deny": [
    { "group": "Destructive", "patterns": ["rm ", "--force", "--hard"] }
  ]
}
```

Rules live in `lib/command_rules.json` and are hot-reloaded. Deny always wins.

To enable, add to your `.env`:

```
COMMAND_RULES=true
```

## Context Monitor (optional)

PocketCursor can monitor your context window fill level and remind the AI to save its working memory before a summary compresses it.

When enabled, it prefills an annotation like `[ContextMonitor: 85% context used -- journal reminder (see pocket-cursor.mdc § Context monitor)]` directly into the chat input field after each AI response. The annotation rides with the user's next message — no extra LLM roundtrip, no separate turn. The AI decides what to do with the reminder (the Cursor rule tells it how). It might write a journal entry at `.pocket-cursor/journals/`, or it might not. The journal is the AI's personal memory space.

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

## Slow image delivery on network drives (optional)

The phone outbox renders styled images via Node.js and Puppeteer. If the repo lives on a network drive with high latency (e.g. NAS over VPN), this can be slow — Node.js loads hundreds of small files from `node_modules` on every render. The fix: give Node.js a local copy of its dependencies.

```bash
python setup_local_render.py /path/to/local/render
```

Then add `RENDER_LOCAL_DIR=/path/to/local/render` to your `.env`. When the network is fast again, remove the line — no code changes needed.

## Requirements

- **Python 3.10+**
- **Node.js 18+** (for markdown-to-image rendering via Puppeteer)
- **Cursor IDE** launched with `--remote-debugging-port=9222`
- **Telegram bot token** (free, via @BotFather)
- **OpenAI API key** (optional, for voice transcription)
- **ElevenLabs API key** (optional, for text-to-speech voice replies)

## License

MIT. Build whatever you want with it.
