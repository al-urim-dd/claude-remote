#!/usr/bin/env python3
"""ClaudeRemote: Gmail remote interface for Claude Code.

Polls Gmail for emails with "cc" subject prefix, feeds them to Claude Code
via subprocess, and replies in the same email thread.

Usage:
    ./bridge.py start   # Start daemon (background)
    ./bridge.py stop    # Stop daemon
    ./bridge.py run     # Run in foreground (for debugging)
"""

import base64
import email.utils
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".claude-remote"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
PROCESSED_FILE = CONFIG_DIR / "processed.txt"
SESSIONS_FILE = CONFIG_DIR / "thread_sessions.json"
PID_FILE = CONFIG_DIR / "bridge.pid"
LOCK_FILE = CONFIG_DIR / "bridge.lock"
LOG_FILE = CONFIG_DIR / "bridge.log"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

POLL_INTERVAL = 30  # seconds
CLAUDE_TIMEOUT = 600  # 10 minutes
MAX_RESPONSE_LEN = 50_000  # chars
CLAUDE_CWD = str(Path.home() / "Projects")
SUBJECT_PREFIX = "cc"
REPLY_SENDER_NAME = "ClaudeRemote"  # Display name on reply emails
ATTACHMENTS_DIR = CONFIG_DIR / "attachments"
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB
ATTACHMENT_MAX_AGE_HOURS = 24
PROGRESS_INTERVAL = 120  # seconds between "still working" emails
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"

HELP_TEXT = """\
Available commands and capabilities:

/help -- Show this help message
/sessions -- List recent Claude Code sessions
/resume <session-id> -- Resume a specific session
/cancel -- Cancel a running task (coming soon)
/summary -- Generate and send a work summary for today
/brief -- Morning briefing with TODOs, calendar, PRs, email, Slack

Regular messages -- Sent to Claude Code for processing
Attachments -- Attach files to emails and Claude will analyze them
Calendar, email, docs -- Claude has access to Google Workspace tools
Multi-turn -- Reply in the same thread to continue a conversation"""

DIGEST_ENABLED = True
DIGEST_HOUR = 8  # Send digest at 8am local time
DIGEST_LAST_SENT_FILE = CONFIG_DIR / "digest_last_sent.txt"

CANCEL_FILE = CONFIG_DIR / "cancel.txt"

RATE_LIMIT_PER_HOUR = 20  # Max Claude invocations per hour
RATE_LIMIT_FILE = CONFIG_DIR / "rate_limit.json"  # Tracks invocation timestamps

SUMMARY_ENABLED = True
SUMMARY_HOUR = 16  # Send work summary at 4pm local time
SUMMARY_LAST_SENT_FILE = CONFIG_DIR / "summary_last_sent.txt"

# Module-level state (set in run_bridge)
_startup_time: datetime | None = None
_messages_processed: int = 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("claude-remote")


def setup_logging(foreground: bool = False):
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    if foreground:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        log.addHandler(stream_handler)


# ---------------------------------------------------------------------------
# Gmail Authentication
# ---------------------------------------------------------------------------


def authenticate():
    """Load or create OAuth credentials. Returns a Gmail API service."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise FileNotFoundError(
                    f"OAuth client secret not found at {CLIENT_SECRET}. "
                    "Run setup.sh first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Gmail Client Functions
# ---------------------------------------------------------------------------


def get_my_email(service) -> str:
    """Get the authenticated user's email address."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def search_messages(service, query: str) -> list[dict]:
    """Search Gmail and return list of {id, threadId}."""
    results = (
        service.users().messages().list(userId="me", q=query).execute()
    )
    return results.get("messages", [])


def get_message(service, msg_id: str) -> dict:
    """Fetch a full message and extract useful fields."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )

    headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}

    # Extract plain text body and attachments
    body = _extract_body(msg["payload"])
    attachment_metas = _extract_attachments(msg["payload"])

    # Internal date is epoch ms
    internal_date = int(msg.get("internalDate", 0))

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "internal_date_ms": internal_date,
        "body": body,
        "attachments": attachment_metas,
        "label_ids": msg.get("labelIds", []),
    }


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    # Fallback: decode body data if present at top level
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    return ""


def strip_quoted_reply(text: str) -> str:
    """Strip Gmail/Outlook quoted reply text from an email body."""
    # Gmail-style: "On ... wrote:" -- may span lines and have blank lines before it
    match = re.search(r'\n\s*On .+?wrote:\s*$', text, re.DOTALL | re.MULTILINE)
    if match:
        text = text[:match.start()]
    # Outlook-style
    match = re.search(r'\n\s*From: .+\n', text)
    if match:
        text = text[:match.start()]
    # Forwarded message blocks
    match = re.search(r'\n-+ ?Forwarded message', text)
    if match:
        text = text[:match.start()]
    # Strip trailing > quoted lines
    lines = text.rstrip().splitlines()
    while lines and lines[-1].lstrip().startswith(">"):
        lines.pop()
    return "\n".join(lines).strip()


def strip_claude_prefix(text: str) -> str:
    """Remove the 'cc' subject prefix if present (case-insensitive)."""
    return re.sub(r"^cc\b\s*", "", text, flags=re.IGNORECASE)


def generate_subject(body: str, max_len: int = 50) -> str:
    """Generate a short subject line from the user's message."""
    first_line = body.split("\n")[0].strip()
    first_line = re.sub(r'^cc\b\s*', '', first_line, flags=re.IGNORECASE)
    if len(first_line) > max_len:
        first_line = first_line[:max_len].rsplit(" ", 1)[0] + "..."
    return f"cc {first_line}" if first_line else "cc conversation"


def _extract_attachments(payload: dict) -> list[dict]:
    """Extract attachment metadata (filename, size, id) from a MIME payload."""
    attachments = []
    for part in payload.get("parts", []):
        filename = part.get("filename")
        if filename:
            body = part.get("body", {})
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId"),
                "data": body.get("data"),
            })
        attachments.extend(_extract_attachments(part))
    return attachments


def download_attachments(service, msg_id: str, attachment_metas: list[dict]) -> list[Path]:
    """Download attachments to disk and return list of file paths."""
    if not attachment_metas:
        return []

    msg_dir = ATTACHMENTS_DIR / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for att in attachment_metas:
        if att["size"] > MAX_ATTACHMENT_SIZE:
            log.warning("Skipping oversized attachment %s (%d bytes)", att["filename"], att["size"])
            continue

        if att["data"]:
            data = base64.urlsafe_b64decode(att["data"])
        elif att["attachment_id"]:
            resp = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=att["attachment_id"])
                .execute()
            )
            data = base64.urlsafe_b64decode(resp["data"])
        else:
            continue

        filepath = msg_dir / att["filename"]
        filepath.write_bytes(data)
        paths.append(filepath)
        log.info("Saved attachment %s (%d bytes)", att["filename"], len(data))

    return paths


def cleanup_old_attachments():
    """Remove attachment directories older than ATTACHMENT_MAX_AGE_HOURS."""
    if not ATTACHMENTS_DIR.exists():
        return
    cutoff = time.time() - ATTACHMENT_MAX_AGE_HOURS * 3600
    for child in ATTACHMENTS_DIR.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def get_thread_history(service, thread_id: str) -> list[dict]:
    """Fetch all messages in a thread, sorted chronologically."""
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    messages = []
    for msg in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
        sender_name, _ = email.utils.parseaddr(headers.get("from", ""))
        body = _extract_body(msg["payload"])
        messages.append({
            "from_name": sender_name,
            "date": headers.get("date", ""),
            "body": body.strip(),
        })
    return messages


def build_thread_context(thread_messages: list[dict]) -> str:
    """Format thread history as a conversation for Claude."""
    parts = []
    for msg in thread_messages:
        sender = msg["from_name"] or "Unknown"
        if sender == REPLY_SENDER_NAME:
            role = "You (Claude, in a previous reply)"
        else:
            role = "User"
        body = msg["body"]
        if body:
            parts.append(f"[{role} -- {msg['date']}]\n{body}")
    return "\n\n---\n\n".join(parts)


def format_html_reply(text: str) -> str:
    """Convert Claude's markdown output to email-safe HTML."""
    import markdown
    html_body = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br"],
    )
    style = (
        "<style>"
        "pre { background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; } "
        "code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 13px; font-family: 'SF Mono', Monaco, Consolas, monospace; } "
        "pre code { background: none; padding: 0; } "
        "table { border-collapse: collapse; margin: 8px 0; } "
        "th, td { border: 1px solid #ddd; padding: 6px 12px; text-align: left; } "
        "th { background: #f6f8fa; } "
        "blockquote { border-left: 3px solid #ddd; margin: 8px 0; padding: 4px 12px; color: #555; }"
        "</style>"
    )
    return (
        "<html><body>\n"
        "<div style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        "font-size: 14px; line-height: 1.6; color: #1a1a1a;\">\n"
        + html_body + "\n"
        "</div>\n"
        + style + "\n"
        "</body></html>"
    )


def send_reply(service, original_msg: dict, body_text: str, my_email: str, override_subject: str = None):
    """Reply to a message in the same thread with a distinct sender name."""
    if override_subject:
        subject = override_subject
    else:
        subject = original_msg["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    # Build references chain for proper threading
    references = original_msg.get("references", "")
    if original_msg.get("message_id"):
        if references:
            references += " " + original_msg["message_id"]
        else:
            references = original_msg["message_id"]

    # Use HTML if the body contains markdown-like content
    has_markdown = any(marker in body_text for marker in ['```', '# ', '**', '- ', '| '])

    if has_markdown:
        msg_mime = MIMEMultipart("alternative")
        msg_mime.attach(MIMEText(body_text, "plain"))
        msg_mime.attach(MIMEText(format_html_reply(body_text), "html"))
    else:
        msg_mime = MIMEText(body_text)

    msg_mime["from"] = f"{REPLY_SENDER_NAME} <{my_email}>"
    msg_mime["to"] = original_msg["from"]
    msg_mime["subject"] = subject
    msg_mime["In-Reply-To"] = original_msg.get("message_id", "")
    msg_mime["References"] = references

    raw = base64.urlsafe_b64encode(msg_mime.as_bytes()).decode("ascii")

    sent = (
        service.users()
        .messages()
        .send(
            userId="me",
            body={"raw": raw, "threadId": original_msg["thread_id"]},
        )
        .execute()
    )
    return sent


def mark_as_read(service, msg_id: str):
    """Remove UNREAD label from a message."""
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ---------------------------------------------------------------------------
# Claude Code Integration
# ---------------------------------------------------------------------------


def _check_cancel(thread_id: str) -> bool:
    """Check if thread_id is in the cancel file; remove it if found."""
    if not CANCEL_FILE.exists():
        return False
    lines = CANCEL_FILE.read_text().strip().splitlines()
    if thread_id in lines:
        remaining = [t for t in lines if t != thread_id]
        CANCEL_FILE.write_text("\n".join(remaining) + "\n" if remaining else "")
        return True
    return False


def invoke_claude(
    message: str,
    session_id: str,
    resume: bool = False,
    on_progress: callable = None,
    thread_id: str = None,
) -> str:
    """Run claude -p as a subprocess, sending progress callbacks while waiting."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Strip nested session guard

    cmd = ["claude", "-p", "--output-format", "text"]
    if resume:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])
    cmd.append(message)

    log.info("Invoking Claude (resume=%s, session=%s)", resume, session_id[:8])

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=CLAUDE_CWD,
        )
        elapsed = 0
        next_progress = PROGRESS_INTERVAL
        while proc.poll() is None:
            time.sleep(1)
            elapsed += 1
            if thread_id and _check_cancel(thread_id):
                proc.kill()
                proc.wait()
                log.info("Cancelled Claude for thread %s", thread_id)
                return "[Cancelled by user]\n\nThe task was cancelled. Send a new message to start fresh."
            if elapsed >= CLAUDE_TIMEOUT:
                proc.kill()
                proc.wait()
                output = (
                    f"[Timed out after {CLAUDE_TIMEOUT // 60} minutes]\n\n"
                    "The task was too long for a single request. You can:\n"
                    "- Reply in this thread to continue where it left off\n"
                    "- Break the task into smaller steps\n"
                    "- Reply /resume to pick up the session"
                )
                log.warning("Claude timed out for session %s", session_id[:8])
                break
            if on_progress and elapsed >= next_progress:
                on_progress(elapsed)
                next_progress += PROGRESS_INTERVAL
        else:
            output = proc.stdout.read().strip()
            if proc.returncode != 0 and not output:
                stderr_text = proc.stderr.read().strip()
                output = (
                    f"[Claude exited with code {proc.returncode}]\n\n"
                    + (f"Error: {stderr_text}\n\n" if stderr_text else "")
                    + "Reply in this thread to retry, or start a new thread with cc prefix."
                )
            if not output:
                output = (
                    "[Claude returned empty output]\n\n"
                    "This sometimes happens with very short tasks. Try rephrasing your request."
                )
    except FileNotFoundError:
        output = (
            "[Error: 'claude' command not found]\n\n"
            "Claude Code doesn't appear to be installed or is not in PATH.\n"
            "Install it: npm install -g @anthropic-ai/claude-code"
        )
        log.error("claude command not found")

    # Truncate if too large
    if len(output) > MAX_RESPONSE_LEN:
        output = output[:MAX_RESPONSE_LEN] + "\n\n[truncated -- response exceeded 50K chars]"

    return output


def list_sessions(count: int = 10) -> str:
    """List recent Claude Code sessions by reading JSONL files from disk."""
    cwd_slug = CLAUDE_CWD.replace("/", "-").replace(".", "-")
    sessions_dir = CLAUDE_SESSIONS_DIR / cwd_slug
    if not sessions_dir.exists():
        return "No sessions found."

    entries = []
    for f in sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:count]:
        first_msg = ""
        for line in f.read_text().splitlines():
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") == "summary":
                first_msg = d.get("summary", "")[:80]
                break
            msg = d.get("message")
            if isinstance(msg, dict) and msg.get("role") == "user" and not first_msg:
                content = msg.get("content", "")
                if isinstance(content, str):
                    first_msg = content.strip().replace("\n", " ")[:80]
                elif isinstance(content, list) and content:
                    c = content[0]
                    first_msg = (c.get("text", "") if isinstance(c, dict) else str(c))[:80]
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        entries.append(f"{f.stem}\n  {mtime:%Y-%m-%d %H:%M}  {first_msg}")

    if not entries:
        return "No sessions found."
    header = "Recent Claude sessions (reply /resume <id> to continue one):\n"
    return header + "\n\n".join(entries)


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------


def load_processed_ids() -> set[str]:
    """Load processed message IDs from file."""
    if not PROCESSED_FILE.exists():
        return set()
    return set(PROCESSED_FILE.read_text().strip().splitlines())


def save_processed_id(msg_id: str):
    """Append a processed message ID to file."""
    with open(PROCESSED_FILE, "a") as f:
        f.write(msg_id + "\n")


def load_thread_sessions() -> dict[str, str]:
    """Load thread->session mapping from JSON file."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_thread_sessions(sessions: dict[str, str]):
    """Save thread->session mapping to JSON file."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def _check_rate_limit() -> tuple[bool, int]:
    """Check if we're within the rate limit. Returns (allowed, remaining)."""
    now = time.time()
    cutoff = now - 3600  # 1 hour window

    timestamps = []
    if RATE_LIMIT_FILE.exists():
        try:
            timestamps = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            timestamps = []

    # Prune old entries
    timestamps = [t for t in timestamps if t > cutoff]

    remaining = RATE_LIMIT_PER_HOUR - len(timestamps)
    return remaining > 0, max(remaining, 0)


def _record_invocation():
    """Record a Claude invocation timestamp for rate limiting."""
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
# Daily Digest
# ---------------------------------------------------------------------------



def _invoke_skill(skill_name: str) -> str:
    """Invoke a Claude Code skill (e.g. /brief, /summary) via claude -p."""
    return invoke_claude(f"/{skill_name}", str(uuid.uuid4()), resume=False)


def _send_scheduled_email(service, my_email: str, subject: str, body: str):
    """Send a styled HTML email to the user."""
    html = format_html_reply(body)
    msg_mime = MIMEMultipart("alternative")
    msg_mime.attach(MIMEText(body, "plain"))
    msg_mime.attach(MIMEText(html, "html"))
    msg_mime["from"] = f"ClaudeRemote <{my_email}>"
    msg_mime["to"] = my_email
    msg_mime["subject"] = subject
    raw = base64.urlsafe_b64encode(msg_mime.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_daily_digest(service, my_email: str, thread_sessions: dict, processed_count: int):
    """Send a morning briefing by invoking the /brief skill."""
    now = datetime.now()

    if DIGEST_LAST_SENT_FILE.exists():
        last_sent = DIGEST_LAST_SENT_FILE.read_text().strip()
        if last_sent == now.strftime("%Y-%m-%d"):
            return

    log.info("Generating morning briefing via /brief skill")
    body = _invoke_skill("brief")

    if not body or body.startswith("["):
        log.warning("Morning briefing failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Morning Briefing -- {now.strftime('%b %d')}", body)
    DIGEST_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent morning briefing")


def _maybe_send_digest(service, my_email, thread_sessions, processed_count):
    """Send digest if it is the right hour and has not been sent today."""
    if not DIGEST_ENABLED:
        return
    now = datetime.now()
    if now.hour != DIGEST_HOUR:
        return
    send_daily_digest(service, my_email, thread_sessions, processed_count)


# ---------------------------------------------------------------------------
# Daily Work Summary
# ---------------------------------------------------------------------------


def send_work_summary(service, my_email: str):
    """Send an end-of-day work summary by invoking the /summary skill."""
    now = datetime.now()

    if SUMMARY_LAST_SENT_FILE.exists():
        last_sent = SUMMARY_LAST_SENT_FILE.read_text().strip()
        if last_sent == now.strftime("%Y-%m-%d"):
            return

    log.info("Generating work summary via /summary skill")
    body = _invoke_skill("summary")

    if not body or body.startswith("["):
        log.warning("Work summary failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Work Summary -- {now.strftime('%b %d')}", body)
    SUMMARY_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent work summary")


def _maybe_send_summary(service, my_email):
    """Send work summary if it is the right hour and has not been sent today."""
    if not SUMMARY_ENABLED:
        return
    now = datetime.now()
    if now.hour != SUMMARY_HOUR:
        return
    send_work_summary(service, my_email)


# ---------------------------------------------------------------------------
# Main Polling Loop
# ---------------------------------------------------------------------------


def run_bridge(foreground: bool = False):
    """Main loop: poll Gmail, process messages, reply."""
    global _startup_time, _messages_processed
    setup_logging(foreground=foreground)
    _messages_processed = 0
    log.info("Starting ClaudeRemote")

    # Authenticate
    try:
        service = authenticate()
    except Exception as e:
        log.error("Authentication failed: %s", e)
        print(f"Authentication failed: {e}", file=sys.stderr)
        sys.exit(1)

    my_email = get_my_email(service)
    log.info("Authenticated as %s", my_email)

    # Load state
    processed_ids = load_processed_ids()
    thread_sessions = load_thread_sessions()

    # Record startup time -- ignore messages older than this
    startup_time_ms = int(time.time() * 1000)
    _startup_time = datetime.now(timezone.utc)
    log.info("Startup time: %s (ignoring older messages)", _startup_time.isoformat())
    log.info("Loaded %d processed IDs, %d thread sessions", len(processed_ids), len(thread_sessions))

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        try:
            log.info("Starting poll cycle")
            _poll_cycle(service, my_email, processed_ids, thread_sessions, startup_time_ms)
            log.info("Poll cycle complete")
        except Exception:
            log.exception("Error in poll cycle")
            # Re-authenticate in case token issues
            try:
                service = authenticate()
            except Exception:
                log.exception("Re-authentication failed")

        # Sleep in small increments so we respond to signals
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("ClaudeRemote stopped")


def _poll_cycle(
    service,
    my_email: str,
    processed_ids: set[str],
    thread_sessions: dict[str, str],
    startup_time_ms: int,
):
    """Single poll iteration."""
    cleanup_old_attachments()
    _maybe_send_digest(service, my_email, thread_sessions, len(processed_ids))
    _maybe_send_summary(service, my_email)

    query = f'subject:"{SUBJECT_PREFIX}" newer_than:1d is:unread'
    messages = search_messages(service, query)

    if not messages:
        return

    log.info("Found %d unread messages matching query", len(messages))

    for msg_stub in messages:
        msg_id = msg_stub["id"]

        if msg_id in processed_ids:
            continue

        # Fetch full message
        msg = get_message(service, msg_id)

        # Sender check: only process emails from ourselves, skip our own replies
        sender_name, sender_email = email.utils.parseaddr(msg["from"])
        if sender_email.lower() != my_email.lower():
            log.info("Skipping message from %s (not self)", sender_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if sender_name == REPLY_SENDER_NAME:
            log.info("Skipping own reply %s (from %s)", msg_id, REPLY_SENDER_NAME)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        # Download attachments (if any)
        attachment_paths = download_attachments(service, msg_id, msg.get("attachments", []))

        # Extract the user's question, stripping Gmail quoted reply text
        body = strip_quoted_reply(msg["body"].strip())
        body = strip_claude_prefix(body)
        subject = strip_claude_prefix(msg["subject"])
        if not body and subject:
            body = subject
        if not body and not attachment_paths:
            log.info("Skipping message %s with empty body and no attachments", msg_id)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        thread_id = msg["thread_id"]
        log.info("Processing message %s in thread %s: %.80s", msg_id, thread_id, body)

        # Built-in commands: /help, /status, /cancel, /sessions, /resume <id>
        if body.lower() == "/help":
            response = HELP_TEXT
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/status":
            global _messages_processed
            uptime = datetime.now(timezone.utc) - _startup_time if _startup_time else None
            if uptime:
                hours, remainder = divmod(int(uptime.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = f"{hours}h {minutes}m {seconds}s"
            else:
                uptime_str = "unknown"
            current_session = thread_sessions.get(thread_id, "none")
            response = (
                f"ClaudeRemote Status\n"
                f"---\n"
                f"Uptime: {uptime_str}\n"
                f"Messages processed: {_messages_processed}\n"
                f"Active threads: {len(thread_sessions)}\n"
                f"This thread's session: {current_session}\n"
                f"PID: {os.getpid()}"
            )
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/cancel":
            with open(CANCEL_FILE, "a") as f:
                f.write(thread_id + "\n")
            response = "Cancel requested. If a task is running in this thread, it will be stopped."
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/summary":
            log.info("Manual work summary requested via /summary skill")
            response = _invoke_skill("summary")
            summary_sent = send_reply(service, msg, response, my_email)
            if summary_sent and "id" in summary_sent:
                processed_ids.add(summary_sent["id"])
                save_processed_id(summary_sent["id"])
            SUMMARY_LAST_SENT_FILE.write_text(datetime.now().strftime("%Y-%m-%d"))
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/brief":
            log.info("Manual morning brief requested via /brief skill")
            response = _invoke_skill("brief")
            brief_sent = send_reply(service, msg, response, my_email)
            if brief_sent and "id" in brief_sent:
                processed_ids.add(brief_sent["id"])
                save_processed_id(brief_sent["id"])
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/sessions":
            response = list_sessions()
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower().startswith("/resume "):
            resume_id = body.split(None, 1)[1].strip()
            session_id = resume_id
            thread_sessions[thread_id] = session_id
            save_thread_sessions(thread_sessions)
            response = invoke_claude(
                "The user just resumed this session from their phone via email. "
                "Briefly summarize what you were working on, and ask how to proceed.",
                session_id, resume=True,
            )
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        # Determine session: resume existing or start new
        resume = thread_id in thread_sessions
        if resume:
            session_id = thread_sessions[thread_id]
        else:
            session_id = str(uuid.uuid4())

        # Build attachment preamble
        att_preamble = ""
        if attachment_paths:
            file_list = "\n".join(f"  - {p.name}: {p}" for p in attachment_paths)
            att_preamble = f"Attached files (read and analyze these):\n{file_list}\n\n"

        # Build the prompt: for resumed sessions just send the latest message,
        # for fresh sessions include the full thread for context
        if resume:
            prompt = att_preamble + body
        else:
            thread_messages = get_thread_history(service, thread_id)
            if len(thread_messages) > 1:
                prompt = (
                    att_preamble
                    + "Here is the email conversation so far:\n\n"
                    + build_thread_context(thread_messages)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = att_preamble + body

        # Progress callback: send "still working" emails while Claude runs
        def on_progress(elapsed_secs):
            mins = elapsed_secs // 60
            try:
                progress_sent = send_reply(service, msg, f"[Still working... ({mins}m elapsed)]", my_email)
                if progress_sent and "id" in progress_sent:
                    processed_ids.add(progress_sent["id"])
                    save_processed_id(progress_sent["id"])
                log.info("Sent progress update at %ds for message %s", elapsed_secs, msg_id)
            except Exception:
                log.exception("Failed to send progress email")

        # Rate limit check
        allowed, remaining = _check_rate_limit()
        if not allowed:
            response = (
                "[Rate limit reached]\n\n"
                f"You've used {RATE_LIMIT_PER_HOUR} Claude invocations in the last hour.\n"
                "Wait a bit and try again, or adjust RATE_LIMIT_PER_HOUR in bridge.py."
            )
            rate_sent = send_reply(service, msg, response, my_email)
            if rate_sent and "id" in rate_sent:
                processed_ids.add(rate_sent["id"])
                save_processed_id(rate_sent["id"])
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        log.info("Rate limit: %d/%d remaining", remaining, RATE_LIMIT_PER_HOUR)

        # Invoke Claude
        response = invoke_claude(prompt, session_id, resume=resume, on_progress=on_progress, thread_id=thread_id)
        _record_invocation()

        # If resume failed, retry with full thread context on a fresh session
        if resume and "[Claude exited with code" in response:
            log.warning("Resume failed for session %s, retrying fresh with thread context", session_id[:8])
            session_id = str(uuid.uuid4())
            thread_messages = get_thread_history(service, thread_id)
            if len(thread_messages) > 1:
                prompt = (
                    att_preamble
                    + "Here is the email conversation so far:\n\n"
                    + build_thread_context(thread_messages)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = att_preamble + body
            response = invoke_claude(prompt, session_id, resume=False, on_progress=on_progress, thread_id=thread_id)
            _record_invocation()

        # Reply in thread -- override subject on first reply in new thread
        try:
            if not resume:
                new_subject = generate_subject(body)
                sent = send_reply(service, msg, response, my_email, override_subject=f"Re: {new_subject}")
            else:
                sent = send_reply(service, msg, response, my_email)
            # Track sent reply ID so we never re-process our own replies
            if sent and "id" in sent:
                processed_ids.add(sent["id"])
                save_processed_id(sent["id"])
            log.info("Replied to message %s (session=%s)", msg_id, session_id[:8])
        except Exception:
            log.exception("Failed to send reply for message %s", msg_id)

        # Update state
        mark_as_read(service, msg_id)
        processed_ids.add(msg_id)
        save_processed_id(msg_id)
        thread_sessions[thread_id] = session_id
        save_thread_sessions(thread_sessions)
        _messages_processed += 1


# ---------------------------------------------------------------------------
# Daemon Management
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_bridge_pids():
    """Return PIDs of running bridge.py daemon processes (not ourselves)."""
    skip_pids = {os.getpid(), os.getppid()}
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        if "bridge.py" not in line or "slack_bridge" in line:
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
    """Start the bridge as a background process.

    Uses fcntl.flock() on LOCK_FILE to guarantee at most one daemon runs.
    The lock is acquired before forking and the fd is inherited by the
    grandchild, so there is no race window where a second instance can
    slip through.
    """
    # Kill any orphan bridge processes before attempting to start.
    orphans = _find_bridge_pids()
    for pid in orphans:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if orphans:
        time.sleep(0.5)
        for pid in orphans:
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    LOCK_FILE.touch(exist_ok=True)
    lock_fd = open(LOCK_FILE, "r+")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        pid = PID_FILE.read_text().strip() if PID_FILE.exists() else "unknown"
        print(f"Bridge already running (PID {pid})")
        return

    # Lock acquired — clean stale PID file.
    if PID_FILE.exists():
        PID_FILE.unlink()

    # Fork into background. Child inherits lock_fd.
    pid = os.fork()
    if pid > 0:
        lock_fd.close()
        print(f"Bridge started (PID {pid})")
        print(f"Logs: {LOG_FILE}")
        return

    # Child: become session leader.
    os.setsid()

    # Second fork to fully detach. Grandchild inherits lock_fd.
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Grandchild — the actual daemon.
    # Keep lock_fd open: flock is per open-file-description, so the lock
    # stays held as long as ANY fd referencing that description is open.

    # Redirect stdio.
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    # Write PID file.
    PID_FILE.write_text(str(os.getpid()))

    try:
        run_bridge(foreground=False)
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        lock_fd.close()


def stop_daemon():
    """Stop all running bridge daemon(s).

    Uses both PID file and pgrep to find processes, ensuring orphans
    are killed too. Falls back to SIGKILL if SIGTERM doesn't work.
    """
    pids = set(_find_bridge_pids())
    if PID_FILE.exists():
        try:
            file_pid = int(PID_FILE.read_text().strip())
            if _pid_alive(file_pid):
                pids.add(file_pid)
        except (ValueError, OSError):
            pass
    pids = list(pids)

    if not pids:
        print("Bridge is not running")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"Sent SIGTERM to bridge process(es): {pids}")

    for _ in range(10):
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(0.5)

    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    print("Bridge stopped")
    if PID_FILE.exists():
        PID_FILE.unlink()


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
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
