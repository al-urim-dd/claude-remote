# ClaudeRemote

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via Gmail and Slack.

A single `bridge.py` daemon polls both transports, invokes Claude Code when messages arrive, and replies in-thread. Polling is free ($0) - you only pay for Claude API calls when there's actual work. All data stays within your environment.

## How It Works

```
Gmail / Slack  ->  bridge.py (polls every 15s)
                       |
                       v
                 claude -p "your question"  (up to 3 concurrent)
                       |
                       v
                 Reply via same transport
```

**Gmail**: send yourself an email with subject prefix "cc". Bridge replies as "ClaudeRemote".

**Slack (private channel)**: post in your private `#your-agent-channel` channel. No prefix needed.

**Slack (any channel)**: type `@ClaudeRemote <task>` in any channel. The bridge searches for mentions every 15s and replies in-thread. Configurable user and channel whitelists control who can trigger it.

## Quick Start

### Gmail Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project -> enable **Gmail API**
3. Go to **OAuth consent screen** -> User type: **Internal** -> fill in app name + email
4. Go to **Credentials** -> **+ Create Credentials** -> **OAuth client ID** -> **Desktop app**
5. Download the JSON file

```bash
git clone https://github.com/zhengli-sun/claude-remote.git
cd claude-remote
mkdir -p ~/.claude-remote
mv ~/Downloads/client_secret_*.json ~/.claude-remote/client_secret.json
./setup.sh
```

### Slack Setup

No bot token or Slack app needed. The bridge uses the Slack MCP OAuth token.

**Option 1: Standalone token (recommended)**

Run the included OAuth helper to get a persistent token:

```bash
cd claude-remote
.venv/bin/python slack_oauth.py
```

This saves a 12-hour token to `~/.claude-remote/slack_mcp_token.json`. The bridge auto-refreshes it before expiry (see [Token Auto-Refresh](#token-auto-refresh)).

**Option 2: Claude Code credentials**

Make sure the Slack MCP plugin is authenticated in Claude Code (run any Slack tool once). The bridge will fall back to `~/.claude/.credentials.json`, but this file is ephemeral and only exists while Claude Code is running.

**Then:**

1. Create a private channel `#your-agent-channel` in Slack
2. Run `/check-slack` in Claude Code to initialize the channel state

### Environment Configuration

Create `~/.claude-remote/env` to override defaults without editing `bridge.py`:

```bash
# Working directory for Claude Code invocations
CLAUDE_REMOTE_CWD="$HOME"

# Slack channel to watch (default: your-agent-channel)
CLAUDE_REMOTE_SLACK_CHANNEL="your-agent-channel"

# Your Slack user ID (required for cross-channel @ClaudeRemote)
CLAUDE_REMOTE_SLACK_USER_ID="U0123456789"

# Additional users who can trigger @ClaudeRemote (comma-separated Slack user IDs)
CLAUDE_REMOTE_ALLOWED_USERS="UABC,UDEF"

# Channels where ANY user can trigger @ClaudeRemote (comma-separated channel IDs)
CLAUDE_REMOTE_OPEN_CHANNELS="C0123456789"

# Auto-refresh Slack token when < N hours remaining (0 to disable)
CLAUDE_REMOTE_SLACK_REFRESH_HOURS=2

# Max concurrent Claude invocations (default: 3)
CLAUDE_REMOTE_MAX_CONCURRENT=3
```

## Usage

```bash
# Gmail only (default, backward compatible)
python3 bridge.py run

# Slack only
python3 bridge.py run --slack

# Both transports
python3 bridge.py run --all

# Background daemon
python3 bridge.py start --all
python3 bridge.py stop
python3 bridge.py status

# Tail logs
tail -f ~/.claude-remote/bridge.log
```

### Gmail

Send an email **to yourself** with the subject:

```
cc your question here
```

The bridge picks it up within ~15 seconds and replies in the same thread.

### Slack (Private Channel)

Post in `#your-agent-channel`:

```
summarize #eng-incidents from the last 2 hours
```

No prefix needed. The bridge reacts with :eyes: (acknowledged), invokes Claude, replies in-thread with :robot_face: prefix, then reacts with :white_check_mark: (done).

### Slack (Cross-Channel)

Type `@ClaudeRemote` followed by your task in **any** channel:

```
@ClaudeRemote summarize the top learnings in this channel over the past week
```

The bridge searches for `@ClaudeRemote` mentions every 15s. Only messages from whitelisted users (or anyone in whitelisted channels) are processed.

Reply in the thread to continue the conversation - but you must include `@ClaudeRemote` each time.

## Commands

Available in both Gmail and Slack:

| Command | Description |
|---------|-------------|
| `help` | Show available commands and capabilities |
| `status` | Bridge health: uptime, messages processed, active threads, PID |
| `cancel` | Cancel a running Claude task in the current thread |

Gmail-only commands:

| Command | Description |
|---------|-------------|
| `/sessions` | List your 10 most recent Claude Code sessions |
| `/resume <id>` | Resume any session by ID |
| `/daily-summary` | Generate an end-of-day work summary |
| `/daily-brief` | Generate a morning briefing |

## Features

### Core
- **Multi-turn conversations** - replies in the same thread share context
- **Session resume** - both Gmail and Slack thread replies reuse the same Claude Code session (`--resume`), preserving full context of prior tool calls and file edits
- **Thread history fallback** - when a session can't be resumed, the bridge fetches full thread history as context and starts a fresh session
- **Google Workspace access** - Claude can read/create calendar events, search Gmail, edit Google Docs, and more via MCP tools
- **Rate limiting** - caps Claude invocations at 20/hour (shared across transports) to prevent runaway costs
- **Concurrent invocations** - up to 3 Claude processes run in parallel via a thread pool, so long-running tasks don't block new requests
- **Safety guardrails** - every invocation is prepended with mandatory rules: no PII exposure, no secret leaks, no production mutations, no unauthorized PR approvals, no destructive git ops

### Cross-Channel Invocation
- **Trigger keyword** - type `@ClaudeRemote <task>` in any Slack channel (literal text, not a real mention)
- **User allowlist** - `CLAUDE_REMOTE_ALLOWED_USERS` controls who can trigger from any channel
- **Open channels** - `CLAUDE_REMOTE_OPEN_CHANNELS` allows any user in specified channels to trigger
- **Automatic search** - bridge searches Slack every 15s for new mentions
- **Requester @mention** - replies @mention the person who asked, not the bridge owner

### Gmail-specific
- **Rich HTML formatting** - replies with code blocks, tables, and headings
- **File attachments** - send images, PDFs, or code files and Claude will analyze them
- **Progress emails** - sends "still working..." replies every 2 minutes for long tasks
- **Smart thread naming** - first reply uses a descriptive subject line
- **Scheduled emails** - daily briefing (8am) and work summary (10pm), with built-in prompts (no external skill files required)

### Slack-specific
- **$0 polling** - reads Slack via direct MCP HTTP calls (no LLM involved in polling)
- **Emoji acknowledgement** - :eyes: on receipt, :white_check_mark: on reply
- **Active thread tracking** - monitors private channel threads for follow-up replies
- **Session resume** - thread replies reuse the same Claude Code session
- **Agent output filtering** - prevents self-reply loops by detecting :robot_face: prefixed messages
- **No bot token needed** - uses Slack MCP OAuth token with auto-refresh

### Token Auto-Refresh
- Slack OAuth tokens expire every 12 hours
- The bridge proactively refreshes the token when it has less than 2 hours remaining (configurable via `CLAUDE_REMOTE_SLACK_REFRESH_HOURS`)
- If refresh fails, the bridge sends a notification to the Slack channel with instructions to re-authenticate
- Set `CLAUDE_REMOTE_SLACK_REFRESH_HOURS=0` to disable auto-refresh

### Thread Lifecycle
- **Stale thread cleanup** - threads that fail to read 3 consecutive times are automatically removed from tracking (resilient to transient errors)
- **Session file cleanup** - when a thread is removed, the associated Claude Code session `.jsonl` file is deleted from disk
- **7-day TTL** - threads older than 7 days are pruned automatically with session cleanup

### Reliability
- **Startup safety** - ignores all pre-existing messages on startup
- **Graceful shutdown** - handles SIGINT/SIGTERM cleanly, waits for in-flight invocations to complete
- **Auto-recovery** - re-authenticates Gmail on token expiry, auto-refreshes Slack token
- **Observability** - session ID and resume flag logged for every message in both Gmail and Slack

## Project Structure

```
claude-remote/
├── bridge.py              # Unified bridge (Gmail + Slack MCP)
├── slack_oauth.py         # Standalone Slack OAuth flow helper
├── test_bridge.py         # Bridge tests (96 tests)
├── test_slack_bridge.py   # Slack-specific tests (37 tests)
├── test_no_secrets.py     # Secret scanner tests (2 tests)
├── setup.sh               # One-time setup script
├── requirements.txt       # Python dependencies
└── README.md

~/.claude-remote/
├── env                     # Environment overrides (CWD, channel, user IDs, etc.)
├── client_secret.json      # Gmail OAuth credentials (you provide)
├── token.json              # Gmail cached OAuth token (auto-generated)
├── processed.txt           # Gmail processed message IDs
├── thread_sessions.json    # Gmail thread -> session mapping
├── slack_mcp_token.json    # Persistent Slack OAuth token (auto-refreshed)
├── slack_agent_state.json  # Slack channel ID, active threads, sessions, processed IDs
├── rate_limit.json         # Shared invocation timestamps (20/hour cap)
├── attachments/            # Downloaded email attachments (auto-cleaned after 24h)
├── bridge.pid              # Daemon PID file
└── bridge.log              # Unified log file
```

## Configuration

Create `~/.claude-remote/env` to override settings, or edit constants at the top of `bridge.py`:

| Setting | Default | Env var | Description |
|---------|---------|---------|-------------|
| `POLL_INTERVAL` | `15` | `CLAUDE_REMOTE_POLL_INTERVAL` | Seconds between polls |
| `CLAUDE_TIMEOUT` | `1800` | - | Max seconds per Claude invocation |
| `MAX_RESPONSE_LEN` | `50000` | - | Truncate replies beyond this |
| `CLAUDE_CWD` | `~/Projects` | `CLAUDE_REMOTE_CWD` | Working directory for Claude |
| `SUBJECT_PREFIX` | `cc` | - | Email subject prefix to watch for |
| `RATE_LIMIT_PER_HOUR` | `20` | - | Max Claude invocations per hour (shared) |
| `MAX_CONCURRENT_INVOCATIONS` | `3` | `CLAUDE_REMOTE_MAX_CONCURRENT` | Max parallel Claude processes |
| `BUSINESS_HOURS_START` | `8` | `CLAUDE_REMOTE_BIZ_START` | Slack business hours start |
| `BUSINESS_HOURS_END` | `22` | `CLAUDE_REMOTE_BIZ_END` | Slack business hours end |
| `BUSINESS_HOURS_ONLY` | `false` | `CLAUDE_REMOTE_BIZ_ONLY` | Gate Slack polls to business hours |
| `SLACK_CHANNEL_NAME` | `your-agent-channel` | `CLAUDE_REMOTE_SLACK_CHANNEL` | Private Slack channel to watch |
| `SLACK_USER_ID` | - | `CLAUDE_REMOTE_SLACK_USER_ID` | Your Slack user ID (required for cross-channel) |
| `CROSS_CHANNEL_ENABLED` | `true` | `CLAUDE_REMOTE_CROSS_CHANNEL` | Enable @ClaudeRemote search |
| `CROSS_CHANNEL_TRIGGER` | `@ClaudeRemote` | `CLAUDE_REMOTE_TRIGGER` | Trigger keyword for cross-channel |
| `CROSS_CHANNEL_ALLOWED_USERS` | - | `CLAUDE_REMOTE_ALLOWED_USERS` | Comma-separated user IDs that can trigger |
| `CROSS_CHANNEL_OPEN_CHANNELS` | - | `CLAUDE_REMOTE_OPEN_CHANNELS` | Comma-separated channel IDs open to all users |
| `SLACK_TOKEN_REFRESH_HOURS` | `2` | `CLAUDE_REMOTE_SLACK_REFRESH_HOURS` | Refresh token when < N hours left (0 = off) |
| `SLACK_NOTIFY_THRESHOLD` | `30` | `CLAUDE_REMOTE_NOTIFY_THRESHOLD` | Seconds before @mentioning requester in reply |

## Security

- **Sender check** (Gmail): only processes emails from your own address
- **Subject gate** (Gmail): only emails with "cc" prefix
- **Private channel** (Slack): only watches your private `#your-agent-channel` channel
- **User allowlist** (cross-channel): only whitelisted Slack user IDs can trigger @ClaudeRemote
- **Channel whitelist** (cross-channel): optionally open specific channels to all users
- **Safety guardrails**: every Claude invocation includes mandatory rules against PII exposure, secret leaks, production mutations, unauthorized PR approvals, destructive git ops, and unsolicited messaging
- **Rate limiting**: 20 invocations/hour cap prevents runaway costs
- **Startup safety**: ignores all existing messages on daemon start
- **Local only**: all processing on your laptop, API calls over HTTPS
- **Token security**: OAuth credentials stored in `~/.claude-remote/` (chmod 600)
