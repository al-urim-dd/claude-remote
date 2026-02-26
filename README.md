# ClaudeRemote

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via Gmail.

Your laptop runs a polling daemon that watches for specially-prefixed emails, feeds them to Claude Code, and replies in the same email thread. All data stays within your environment (company Gmail + local laptop).

## How It Works

```
Phone (Gmail)  →  Gmail Server  →  bridge.py (polls every 30s)
                                        │
                                        ▼
                                  claude -p "your question"
                                        │
                                        ▼
                                  Gmail API reply
                                        │
Phone (Gmail)  ←  Gmail Server  ←───────┘
```

**You** send emails as yourself. **ClaudeRemote** replies with the display name "ClaudeRemote" so you can tell them apart at a glance.

## Quick Start

### 1. Get Gmail API Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project → enable **Gmail API**
3. Go to **OAuth consent screen** → User type: **Internal** → fill in app name + email
4. Go to **Credentials** → **+ Create Credentials** → **OAuth client ID** → **Desktop app**
5. Download the JSON file

### 2. Setup

```bash
git clone https://github.com/zhengli-sun/claude-remote.git
cd claude-remote

# Place your credentials
mkdir -p ~/.claude-remote
mv ~/Downloads/client_secret_*.json ~/.claude-remote/client_secret.json

# Run setup (creates venv, installs deps, opens browser for OAuth consent)
./setup.sh
```

### 3. Run

```bash
# Foreground (for testing — see logs in terminal)
./bridge.py run

# Background daemon
./bridge.py start
./bridge.py stop

# Tail logs
tail -f ~/.claude-remote/bridge.log
```

### 4. Use

Send an email **to yourself** with the subject:

```
[claude] your question here
```

The bridge picks it up within ~30 seconds and replies in the same thread. Reply in the thread to continue the conversation (Claude retains context via sessions).

## Features

- **Multi-turn conversations** — replies in the same thread share a Claude session
- **Thread history context** — when a session can't be resumed (e.g. after restart), the bridge fetches the full email thread and sends prior messages as context
- **File attachments** — send images, PDFs, or code files as email attachments and Claude will read and analyze them
- **Progress emails** — for long-running tasks, sends "still working..." replies every 2 minutes so you know the request wasn't lost
- **Session management** — email `/sessions` to list recent Claude sessions, `/resume <id>` to resume any session (including ones started from the terminal)
- **Read-tolerant** — uses time-based search instead of unread status, so accidentally opening an email won't cause it to be missed
- **Quoted reply stripping** — strips Gmail/Outlook quoted reply text so Claude only sees your new message
- **Distinct sender** — bridge replies show as "ClaudeRemote" so you can tell them apart from your own emails
- **Startup safety** — ignores all pre-existing emails on startup (no stale replay)
- **Self-only** — only processes emails sent from your own address
- **Auto-recovery** — re-authenticates on token expiry, retries fresh session on resume failure
- **Graceful shutdown** — handles SIGINT/SIGTERM cleanly

## Commands

| Command | Description |
|---------|-------------|
| `/sessions` | List your 10 most recent Claude Code sessions (ID, timestamp, first message) |
| `/resume <session-id>` | Resume any session by ID — including ones started from the terminal |

## Project Structure

```
claude-remote/
├── bridge.py           # Main daemon: poll, process, reply
├── setup.sh            # One-time setup script
├── requirements.txt    # Python dependencies
└── README.md

~/.claude-remote/
├── client_secret.json   # OAuth credentials (you provide this)
├── token.json           # Cached OAuth token (auto-generated)
├── processed.txt        # Processed message IDs
├── thread_sessions.json # Thread → Claude session mapping
├── attachments/         # Downloaded email attachments (auto-cleaned after 24h)
├── bridge.pid           # PID file (when running as daemon)
└── bridge.log           # Log file
```

## Configuration

Edit the constants at the top of `bridge.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL` | `30` | Seconds between Gmail polls |
| `CLAUDE_TIMEOUT` | `600` | Max seconds per Claude invocation |
| `MAX_RESPONSE_LEN` | `50000` | Truncate replies beyond this length |
| `CLAUDE_CWD` | `~/Projects` | Working directory for Claude |
| `SUBJECT_PREFIX` | `[claude]` | Email subject prefix to watch for |
| `REPLY_SENDER_NAME` | `ClaudeRemote` | Display name on reply emails |
| `MAX_ATTACHMENT_SIZE` | `10MB` | Skip attachments larger than this |
| `PROGRESS_INTERVAL` | `120` | Seconds between "still working" progress emails |

## Security

- **Sender check**: only processes emails from your own address
- **Subject gate**: only emails with `[claude]` prefix
- **Startup safety**: ignores all existing unread emails on daemon start
- **Local only**: all processing on your laptop, Gmail API over HTTPS
- **Token security**: OAuth credentials stored in `~/.claude-remote/` (chmod 700)
