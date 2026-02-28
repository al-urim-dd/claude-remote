# ClaudeRemote

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via Gmail (and soon Slack).

Your laptop runs a polling daemon that watches for specially-prefixed emails, feeds them to Claude Code, and replies in the same email thread. All data stays within your environment (company Gmail + local laptop).

## How It Works

```
Phone (Gmail)  ->  Gmail Server  ->  bridge.py (polls every 30s)
                                        |
                                        v
                                  claude -p "your question"
                                        |
                                        v
                                  Gmail API reply (HTML formatted)
                                        |
Phone (Gmail)  <-  Gmail Server  <------+
```

**You** send emails as yourself. **ClaudeRemote** replies with the display name "ClaudeRemote" so you can tell them apart at a glance.

## Quick Start

### 1. Get Gmail API Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project -> enable **Gmail API**
3. Go to **OAuth consent screen** -> User type: **Internal** -> fill in app name + email
4. Go to **Credentials** -> **+ Create Credentials** -> **OAuth client ID** -> **Desktop app**
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
# Foreground (for testing -- see logs in terminal)
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
cc your question here
```

The bridge picks it up within ~30 seconds and replies in the same thread. Reply in the thread to continue the conversation (Claude retains context via sessions).

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands and capabilities |
| `/status` | Bridge health: uptime, messages processed, active threads, PID |
| `/sessions` | List your 10 most recent Claude Code sessions |
| `/resume <id>` | Resume any session by ID (including ones started from the terminal) |
| `/cancel` | Cancel a running Claude task in the current thread |
| `/summary` | Generate an end-of-day work summary (PRs, docs, calendar) |

## Features

### Core
- **Multi-turn conversations** -- replies in the same thread share a Claude session
- **Thread history context** -- when a session can't be resumed (e.g. after restart), the bridge fetches the full email thread and sends prior messages as context
- **Smart thread naming** -- first reply uses a descriptive subject line based on your question
- **File attachments** -- send images, PDFs, or code files as email attachments and Claude will read and analyze them
- **Google Workspace access** -- Claude can read/create calendar events, search Gmail, edit Google Docs, and more via MCP tools

### Email Quality
- **Rich HTML formatting** -- replies with code blocks, tables, and headings render as styled HTML in Gmail
- **Quoted reply stripping** -- strips Gmail/Outlook quoted reply text so Claude only sees your new message
- **`cc` prefix stripping** -- removes the prefix before sending to Claude so it gets a clean prompt
- **Distinct sender** -- bridge replies show as "ClaudeRemote" so you can tell them apart

### Reliability
- **Progress emails** -- for long-running tasks, sends "still working..." replies every 2 minutes
- **Rate limiting** -- caps Claude invocations at 20/hour to prevent runaway costs
- **Actionable errors** -- timeout, crash, and empty output errors include recovery steps
- **Auto-recovery** -- re-authenticates on token expiry, retries fresh session on resume failure
- **Startup safety** -- ignores all pre-existing emails on startup (no stale replay)
- **Graceful shutdown** -- handles SIGINT/SIGTERM cleanly

### Scheduled Emails
- **Daily digest** (5am GMT) -- bridge stats + PRs awaiting your review (via `gh` CLI)
- **Work summary** (4pm local) -- AI-generated summary of your day: PRs, Google Docs, calendar meetings, key activities

## Slack Bridge (Beta)

A Slack bridge (`slack_bridge.py`) is also available. Same architecture -- mention the bot in a channel, it invokes Claude and replies in the thread.

```bash
# Setup: save your Slack Bot Token
echo "xoxb-your-token" > ~/.claude-remote/slack_token.txt

# Set the channel to watch
export CLAUDE_REMOTE_SLACK_CHANNEL=C0123456789

# Run
./slack_bridge.py start
```

See [PR #16](https://github.com/zhengli-sun/claude-remote/pull/16) for full setup instructions.

## Project Structure

```
claude-remote/
├── bridge.py           # Gmail bridge daemon
├── slack_bridge.py     # Slack bridge daemon
├── test_bridge.py      # Gmail bridge tests (63 tests)
├── test_slack_bridge.py # Slack bridge tests (44 tests)
├── setup.sh            # One-time setup script
├── requirements.txt    # Python dependencies
└── README.md

~/.claude-remote/
├── client_secret.json   # Gmail OAuth credentials (you provide)
├── token.json           # Cached OAuth token (auto-generated)
├── slack_token.txt      # Slack bot token (you provide)
├── processed.txt        # Gmail processed message IDs
├── slack_processed.txt  # Slack processed message IDs
├── thread_sessions.json # Gmail thread -> session mapping
├── slack_sessions.json  # Slack thread -> session mapping
├── rate_limit.json      # Invocation timestamps for rate limiting
├── attachments/         # Downloaded email attachments (auto-cleaned after 24h)
├── bridge.pid           # Gmail bridge PID file
├── slack_bridge.pid     # Slack bridge PID file
├── bridge.log           # Gmail bridge log
└── slack_bridge.log     # Slack bridge log
```

## Configuration

Edit the constants at the top of `bridge.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL` | `30` | Seconds between Gmail polls |
| `CLAUDE_TIMEOUT` | `600` | Max seconds per Claude invocation |
| `MAX_RESPONSE_LEN` | `50000` | Truncate replies beyond this length |
| `CLAUDE_CWD` | `~/Projects` | Working directory for Claude |
| `SUBJECT_PREFIX` | `cc` | Email subject prefix to watch for |
| `REPLY_SENDER_NAME` | `ClaudeRemote` | Display name on reply emails |
| `MAX_ATTACHMENT_SIZE` | `10MB` | Skip attachments larger than this |
| `PROGRESS_INTERVAL` | `120` | Seconds between "still working" progress emails |
| `RATE_LIMIT_PER_HOUR` | `20` | Max Claude invocations per hour |
| `DIGEST_HOUR` | `5` | Hour (GMT) to send daily digest |
| `SUMMARY_HOUR` | `16` | Hour (local) to send work summary |

## Security

- **Sender check**: only processes emails from your own address
- **Subject gate**: only emails with `cc` prefix
- **Rate limiting**: 20 invocations/hour cap prevents runaway costs
- **Startup safety**: ignores all existing emails on daemon start
- **Local only**: all processing on your laptop, Gmail API over HTTPS
- **Token security**: OAuth credentials stored in `~/.claude-remote/` (chmod 700)
