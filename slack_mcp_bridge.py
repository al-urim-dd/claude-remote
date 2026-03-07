#!/usr/bin/env python3
"""ClaudeRemote Slack MCP Bridge: polls Slack via MCP (no bot token needed).

Uses the Slack MCP OAuth token already stored by Claude Code to poll a
private channel for new messages. Only invokes Claude when there is actual
work to do - keeping polling cost at $0.

Usage:
    ./slack_mcp_bridge.py start   # Start daemon (background)
    ./slack_mcp_bridge.py stop    # Stop daemon
    ./slack_mcp_bridge.py run     # Run in foreground (for debugging)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".claude-remote"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
AGENT_STATE_FILE = CONFIG_DIR / "slack_agent_state.json"
MCP_URL = "https://mcp.slack.com/mcp"
POLL_INTERVAL = int(os.environ.get("CLAUDE_REMOTE_POLL_INTERVAL", "30"))
CLAUDE_TIMEOUT = 600  # 10 minutes
MAX_RESPONSE_LEN = 50_000
CLAUDE_CWD = str(Path.home() / "Projects")
AGENT_PREFIX = ":robot_face:"
BUSINESS_HOURS_START = int(os.environ.get("CLAUDE_REMOTE_BIZ_START", "8"))
BUSINESS_HOURS_END = int(os.environ.get("CLAUDE_REMOTE_BIZ_END", "22"))
BUSINESS_HOURS_ONLY = os.environ.get("CLAUDE_REMOTE_BIZ_ONLY", "false").lower() == "true"
RATE_LIMIT_PER_HOUR = 20
RATE_LIMIT_FILE = CONFIG_DIR / "slack_rate_limit.json"
PID_FILE = CONFIG_DIR / "slack_mcp_bridge.pid"
LOCK_FILE = CONFIG_DIR / "slack_mcp_bridge.lock"
LOG_FILE = CONFIG_DIR / "slack_mcp_bridge.log"
CANCEL_FILE = CONFIG_DIR / "slack_cancel.txt"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("slack-mcp-bridge")


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every emit."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging(foreground: bool = False):
    # Clear any existing handlers (important after fork)
    log.handlers.clear()
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = _FlushFileHandler(LOG_FILE, mode="a")
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    log.addHandler(fh)
    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(formatter)
        log.addHandler(sh)


# ---------------------------------------------------------------------------
# MCP Token Management
# ---------------------------------------------------------------------------


def _load_credentials() -> dict:
    """Load the credentials file."""
    if not CREDENTIALS_FILE.exists():
        log.error("Credentials file not found: %s", CREDENTIALS_FILE)
        return {}
    return json.loads(CREDENTIALS_FILE.read_text())


def _find_slack_token_entry(creds: dict) -> dict | None:
    """Find the active Slack MCP OAuth entry."""
    mcp_oauth = creds.get("mcpOAuth", {})
    for key, entry in mcp_oauth.items():
        if "slack" not in key:
            continue
        server_url = entry.get("serverUrl", "")
        access_token = entry.get("accessToken", "")
        if "mcp.slack.com/mcp" in server_url and access_token:
            return entry
    return None


def get_slack_token() -> str | None:
    """Get the current Slack OAuth access token from Claude Code credentials."""
    creds = _load_credentials()
    entry = _find_slack_token_entry(creds)
    if not entry:
        log.error("No active Slack MCP token found in credentials")
        return None

    # Check expiry
    expires_at = entry.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    if expires_at and now_ms > expires_at:
        log.warning(
            "Slack token expired at %s. Run any Slack MCP tool in Claude Code "
            "to refresh it, then restart the bridge.",
            datetime.fromtimestamp(expires_at / 1000).isoformat(),
        )
        return None

    return entry["accessToken"]


# ---------------------------------------------------------------------------
# MCP Slack API (direct HTTP, no LLM)
# ---------------------------------------------------------------------------


def _mcp_call(tool_name: str, arguments: dict, token: str) -> dict | None:
    """Call a Slack MCP tool via HTTP JSON-RPC."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    req = Request(
        MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            if "error" in body:
                log.error("MCP error: %s", body["error"])
                return None
            return body.get("result", body)
    except HTTPError as e:
        log.error("MCP HTTP error %d: %s", e.code, e.read().decode()[:500])
        return None
    except (URLError, TimeoutError) as e:
        log.error("MCP connection error: %s", e)
        return None


def _extract_mcp_text(result: dict) -> str | None:
    """Extract the text payload from an MCP tool result.

    MCP returns: {"content": [{"type": "text", "text": "<json-string>"}]}
    The text field is itself a JSON string like {"messages": "..."}.
    We unwrap both layers and return the inner messages string.
    """
    if not isinstance(result, dict):
        return None
    # Extract text from content array
    raw_text = None
    if "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                raw_text = item["text"]
                break
    if raw_text is None:
        return None
    # The text is a JSON string - parse it and extract the messages field
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and "messages" in parsed:
            return parsed["messages"]
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: return raw text as-is
    return raw_text


def mcp_read_channel(token: str, channel_id: str, oldest: str = "", limit: int = 50) -> str | None:
    """Read channel messages via MCP."""
    args = {"channel_id": channel_id, "limit": limit}
    if oldest:
        args["oldest"] = oldest
    result = _mcp_call("slack_read_channel", args, token)
    if result is None:
        return None
    return _extract_mcp_text(result)


def mcp_read_thread(token: str, channel_id: str, message_ts: str) -> str | None:
    """Read thread replies via MCP."""
    result = _mcp_call("slack_read_thread", {
        "channel_id": channel_id,
        "message_ts": message_ts,
    }, token)
    if result is None:
        return None
    return _extract_mcp_text(result)


def mcp_send_message(token: str, channel_id: str, thread_ts: str, message: str) -> bool:
    """Send a message via MCP."""
    result = _mcp_call("slack_send_message", {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "message": message,
    }, token)
    return result is not None


def mcp_add_reaction(token: str, channel_id: str, message_ts: str, emoji: str = "eyes") -> bool:
    """Add an emoji reaction to a message to acknowledge receipt.

    Uses Slack Web API directly since the MCP server doesn't expose
    reactions.add. The MCP OAuth token (xoxe.xoxp-*) works with the
    Slack API.
    """
    payload = json.dumps({
        "channel": channel_id,
        "timestamp": message_ts,
        "name": emoji,
    }).encode()
    req = Request(
        "https://slack.com/api/reactions.add",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if not body.get("ok"):
                log.warning("reactions.add failed: %s", body.get("error", "unknown"))
                return False
            return True
    except (HTTPError, URLError) as e:
        log.warning("reactions.add error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Message Parsing
# ---------------------------------------------------------------------------


def parse_new_messages(channel_text: str, since_ts: str) -> list[dict]:
    """Parse channel read output for new messages after since_ts."""
    messages = []
    current = {}
    for line in channel_text.splitlines():
        if line.startswith("=== Message from "):
            if current:
                messages.append(current)
            current = {"user": "", "ts": "", "text_lines": []}
            # Extract user
            parts = line.split("(")
            if len(parts) > 1:
                current["user"] = parts[1].split(")")[0]
        elif line.startswith("Message TS: "):
            current["ts"] = line.replace("Message TS: ", "").strip()
        elif current:
            current["text_lines"].append(line)

    if current:
        messages.append(current)

    # Filter to messages newer than since_ts, join text lines
    result = []
    for msg in messages:
        ts = msg.get("ts", "")
        if not ts:
            continue
        try:
            if float(ts) <= float(since_ts):
                continue
        except ValueError:
            continue
        text = "\n".join(msg["text_lines"]).strip()
        result.append({"user": msg["user"], "ts": ts, "text": text})

    return result


def parse_thread_replies(thread_text: str, since_ts: str) -> list[dict]:
    """Parse thread read output for new replies after since_ts."""
    replies = []
    current = {}
    in_replies = False
    for line in thread_text.splitlines():
        if "THREAD REPLIES" in line:
            in_replies = True
            continue
        if not in_replies:
            continue
        if line.startswith("--- Reply "):
            if current:
                replies.append(current)
            current = {"user": "", "ts": "", "text_lines": []}
        elif line.startswith("From: "):
            parts = line.split("(")
            if len(parts) > 1:
                current["user"] = parts[1].split(")")[0]
        elif line.startswith("Message TS: "):
            current["ts"] = line.replace("Message TS: ", "").strip()
        elif current:
            current["text_lines"].append(line)

    if current:
        replies.append(current)

    result = []
    for reply in replies:
        ts = reply.get("ts", "")
        if not ts:
            continue
        try:
            if float(ts) <= float(since_ts):
                continue
        except ValueError:
            continue
        text = "\n".join(reply["text_lines"]).strip()
        result.append({"user": reply["user"], "ts": ts, "text": text})

    return result


def should_process(msg: dict) -> bool:
    """Check if a message should be processed."""
    text = msg.get("text", "")
    # Skip agent output - check multiple markers to prevent self-reply loops
    if AGENT_PREFIX in text:
        return False
    if "Sent using" in text and "Claude" in text:
        return False
    if "<@U09RKUYGCM6" in text:
        return False
    if "has joined the channel" in text:
        return False
    if not text.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Claude Code Integration
# ---------------------------------------------------------------------------


def _check_cancel(thread_id: str) -> bool:
    """Check if thread_id is in the cancel file."""
    if not CANCEL_FILE.exists():
        return False
    lines = CANCEL_FILE.read_text().strip().splitlines()
    if thread_id in lines:
        remaining = [t for t in lines if t != thread_id]
        CANCEL_FILE.write_text("\n".join(remaining) + "\n" if remaining else "")
        return True
    return False


def invoke_claude(message: str, thread_id: str = None) -> str:
    """Run claude -p as a subprocess."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    cmd = ["claude", "-p", "--output-format", "text", message]

    log.info("Invoking Claude: %.80s", message)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=CLAUDE_CWD,
        )
        elapsed = 0
        while proc.poll() is None:
            time.sleep(1)
            elapsed += 1
            if thread_id and _check_cancel(thread_id):
                proc.kill()
                proc.wait()
                return "[Cancelled by user]"
            if elapsed >= CLAUDE_TIMEOUT:
                proc.kill()
                proc.wait()
                return f"[Timed out after {CLAUDE_TIMEOUT // 60} minutes]"
        else:
            output = proc.stdout.read().strip()
            if proc.returncode != 0 and not output:
                stderr_text = proc.stderr.read().strip()
                output = f"[Claude exited with code {proc.returncode}]"
                if stderr_text:
                    output += f"\n{stderr_text}"
            if not output:
                output = "[Claude returned empty output]"
    except FileNotFoundError:
        output = "[Error: 'claude' command not found]"
        log.error("claude command not found")

    if len(output) > MAX_RESPONSE_LEN:
        output = output[:MAX_RESPONSE_LEN] + "\n\n[truncated]"

    return output


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def _check_rate_limit() -> tuple[bool, int]:
    now = time.time()
    cutoff = now - 3600
    timestamps = []
    if RATE_LIMIT_FILE.exists():
        try:
            timestamps = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            timestamps = []
    timestamps = [t for t in timestamps if t > cutoff]
    remaining = RATE_LIMIT_PER_HOUR - len(timestamps)
    return remaining > 0, max(remaining, 0)


def _record_invocation():
    now = time.time()
    cutoff = now - 3600
    timestamps = []
    if RATE_LIMIT_FILE.exists():
        try:
            timestamps = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            timestamps = []
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    RATE_LIMIT_FILE.write_text(json.dumps(timestamps))


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if not AGENT_STATE_FILE.exists():
        return {
            "channel_id": "",
            "channel_name": "zhengli-agent",
            "last_checked_ts": "",
            "active_threads": {},
        }
    try:
        return json.loads(AGENT_STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"channel_id": "", "channel_name": "zhengli-agent", "last_checked_ts": "", "active_threads": {}}


def save_state(state: dict):
    AGENT_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Business Hours
# ---------------------------------------------------------------------------


def is_business_hours() -> bool:
    if not BUSINESS_HOURS_ONLY:
        return True
    hour = datetime.now().hour
    return BUSINESS_HOURS_START <= hour < BUSINESS_HOURS_END


# ---------------------------------------------------------------------------
# Main Polling Loop
# ---------------------------------------------------------------------------

_startup_time: datetime | None = None
_messages_processed: int = 0


def run_bridge(foreground: bool = False):
    global _startup_time, _messages_processed
    setup_logging(foreground=foreground)
    _messages_processed = 0
    _startup_time = datetime.now(timezone.utc)

    log.info("Starting Slack MCP Bridge (poll every %ds)", POLL_INTERVAL)
    if BUSINESS_HOURS_ONLY:
        log.info("Business hours: %d:00 - %d:00", BUSINESS_HOURS_START, BUSINESS_HOURS_END)

    # Validate token
    token = get_slack_token()
    if not token:
        print(
            "Error: No valid Slack MCP token found.\n"
            "Run any Slack MCP tool in Claude Code first to authenticate,\n"
            "then start the bridge again.",
            file=sys.stderr,
        )
        sys.exit(1)
    log.info("Slack MCP token loaded successfully")

    # Load state
    state = load_state()
    if not state["channel_id"]:
        print(
            "Error: No channel_id in state file.\n"
            "Run /check-slack in Claude Code first to initialize.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not state["last_checked_ts"]:
        state["last_checked_ts"] = f"{time.time():.6f}"
        save_state(state)
        log.info("First run - initialized timestamp")

    log.info(
        "Watching #%s (%s), %d active threads",
        state["channel_name"],
        state["channel_id"],
        len(state.get("active_threads", {})),
    )

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        if not is_business_hours():
            for _ in range(60):
                if not running:
                    break
                time.sleep(1)
            continue

        try:
            # Re-read token each cycle in case Claude Code refreshed it
            token = get_slack_token()
            if not token:
                log.warning("Token expired or missing, sleeping 60s")
                time.sleep(60)
                continue

            state = load_state()
            _poll_cycle(token, state)
        except Exception:
            log.exception("Error in poll cycle")

        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("Slack MCP Bridge stopped")


def _poll_cycle(token: str, state: dict):
    """Single poll iteration."""
    global _messages_processed
    channel_id = state["channel_id"]
    last_ts = state["last_checked_ts"]
    active_threads = state.get("active_threads", {})

    # 1. Check for new top-level messages
    channel_text = mcp_read_channel(token, channel_id, oldest=last_ts)
    if channel_text is None:
        log.warning("Failed to read channel")
        return

    new_top_level = parse_new_messages(channel_text, last_ts)

    # 2. Check active threads for new replies
    new_thread_replies = []
    for thread_ts, last_reply_ts in list(active_threads.items()):
        thread_text = mcp_read_thread(token, channel_id, thread_ts)
        if thread_text is None:
            continue
        replies = parse_thread_replies(thread_text, last_reply_ts)
        for reply in replies:
            reply["thread_ts"] = thread_ts
            reply["thread_context"] = thread_text
        new_thread_replies.extend(replies)

    # 3. Filter
    to_process = []
    for msg in new_top_level:
        if should_process(msg):
            msg["is_thread_reply"] = False
            to_process.append(msg)

    for reply in new_thread_replies:
        if should_process(reply):
            reply["is_thread_reply"] = True
            to_process.append(reply)

    # Sort chronologically
    to_process.sort(key=lambda m: float(m.get("ts", "0")))

    if not to_process:
        # Update last_checked_ts even when nothing to process
        newest_ts = last_ts
        for msg in new_top_level:
            if float(msg.get("ts", "0")) > float(newest_ts):
                newest_ts = msg["ts"]
        if newest_ts != last_ts:
            state["last_checked_ts"] = newest_ts
            save_state(state)
        return

    log.info(
        "Found %d messages to process (%d top-level, %d thread replies)",
        len(to_process),
        sum(1 for m in to_process if not m["is_thread_reply"]),
        sum(1 for m in to_process if m["is_thread_reply"]),
    )

    # 4. Process each message
    replied = 0
    for msg in to_process:
        # Rate limit
        allowed, remaining = _check_rate_limit()
        if not allowed:
            log.warning("Rate limit reached, skipping remaining messages")
            mcp_send_message(
                token, channel_id,
                msg.get("thread_ts", msg["ts"]),
                f"{AGENT_PREFIX} Rate limit reached (20/hour). Try again later.",
            )
            break

        text = msg["text"]
        thread_ts = msg.get("thread_ts", msg["ts"]) if msg["is_thread_reply"] else msg["ts"]

        # Acknowledge receipt with eyes emoji
        mcp_add_reaction(token, channel_id, msg["ts"], "eyes")

        log.info("Processing: %.80s", text)

        # Build prompt for Claude
        if msg["is_thread_reply"]:
            thread_context = msg.get("thread_context", "")
            prompt = (
                f"You are an AI assistant replying in a Slack thread. "
                f"Here is the full thread context:\n\n{thread_context}\n\n"
                f"The latest message is: {text}\n\n"
                f"Respond to this latest message. Use Slack mrkdwn formatting "
                f"(*bold*, _italic_, `code`). Be concise and helpful. "
                f"Use all available MCP tools if needed."
            )
        else:
            prompt = (
                f"You are an AI assistant responding to a Slack message. "
                f"The message is: {text}\n\n"
                f"Process this as a task. Use all available MCP tools "
                f"(Slack, Google Workspace, Glean, etc.) as needed. "
                f"Use Slack mrkdwn formatting (*bold*, _italic_, `code`). "
                f"Be concise and helpful."
            )

        _record_invocation()
        response = invoke_claude(prompt, thread_id=thread_ts)

        # Post reply
        reply_text = f"{AGENT_PREFIX} {response}"
        success = mcp_send_message(token, channel_id, thread_ts, reply_text)

        if success:
            replied += 1
            log.info("Replied to message %s", msg["ts"])
            # Mark done with checkmark
            mcp_add_reaction(token, channel_id, msg["ts"], "white_check_mark")
            # Track thread
            active_threads[thread_ts] = msg["ts"]
        else:
            log.error("Failed to reply to message %s", msg["ts"])

        _messages_processed += 1

    # 5. Update state
    newest_top_ts = last_ts
    for msg in new_top_level:
        if float(msg.get("ts", "0")) > float(newest_top_ts):
            newest_top_ts = msg["ts"]
    if newest_top_ts != last_ts:
        state["last_checked_ts"] = newest_top_ts

    # Update thread tracking
    for msg in to_process:
        if msg["is_thread_reply"]:
            thread_ts = msg["thread_ts"]
            active_threads[thread_ts] = msg["ts"]
        else:
            active_threads[msg["ts"]] = msg["ts"]

    # Prune stale threads (>7 days)
    cutoff = time.time() - 7 * 86400
    active_threads = {
        k: v for k, v in active_threads.items()
        if float(v) > cutoff
    }

    state["active_threads"] = active_threads
    save_state(state)

    log.info("Processed %d, replied to %d", len(to_process), replied)


# ---------------------------------------------------------------------------
# Daemon Management (reused from slack_bridge.py)
# ---------------------------------------------------------------------------


def _find_bridge_pids():
    skip_pids = {os.getpid(), os.getppid()}
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        if "slack_mcp_bridge.py" not in line:
            continue
        parts = line.split()
        try:
            pid = int(parts[1])
        except (IndexError, ValueError):
            continue
        if pid in skip_pids:
            continue
        pids.append(pid)
    return pids


def start_daemon():
    """Start the bridge as a background process using subprocess.

    Avoids os.fork() which breaks Python logging file handlers.
    """
    # Check if already running
    pids = _find_bridge_pids()
    if pids:
        print(f"Slack MCP bridge already running (PIDs {pids})")
        return

    # Launch as a detached subprocess
    proc = subprocess.Popen(
        [sys.executable, __file__, "run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"Slack MCP bridge started (PID {proc.pid})")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    if BUSINESS_HOURS_ONLY:
        print(f"  Business hours: {BUSINESS_HOURS_START}:00 - {BUSINESS_HOURS_END}:00")
    print(f"  Logs: {LOG_FILE}")


def stop_daemon():
    pids = _find_bridge_pids()
    if pids:
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        print(f"Sent SIGTERM to Slack MCP bridge (PIDs {pids})")
        for _ in range(10):
            alive = any(_safe_kill_check(p) for p in pids)
            if not alive:
                break
            time.sleep(0.5)
        print("Slack MCP bridge stopped")
    else:
        print("Slack MCP bridge is not running")
    if PID_FILE.exists():
        PID_FILE.unlink()


def _safe_kill_check(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status():
    """Show bridge status."""
    pids = _find_bridge_pids()
    if pids:
        print(f"Slack MCP bridge is running (PIDs {pids})")
    else:
        print("Slack MCP bridge is not running")

    state = load_state()
    print(f"  Channel: #{state.get('channel_name', '?')} ({state.get('channel_id', '?')})")
    print(f"  Last checked: {state.get('last_checked_ts', 'never')}")
    print(f"  Active threads: {len(state.get('active_threads', {}))}")

    token = get_slack_token()
    if token:
        creds = _load_credentials()
        entry = _find_slack_token_entry(creds)
        if entry:
            expires = entry.get("expiresAt", 0)
            if expires:
                exp_dt = datetime.fromtimestamp(expires / 1000)
                remaining = exp_dt - datetime.now()
                print(f"  Token expires: {exp_dt.isoformat()} ({remaining})")
    else:
        print("  Token: MISSING or EXPIRED")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "start":
        start_daemon()
    elif command == "stop":
        stop_daemon()
    elif command == "run":
        run_bridge(foreground=True)
    elif command == "status":
        status()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
