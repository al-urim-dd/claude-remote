#!/Users/zhengli.sun/Projects/claude-remote/.venv/bin/python
"""ClaudeRemote: Gmail remote interface for Claude Code.

Polls Gmail for emails with [claude] subject prefix, feeds them to Claude Code
via subprocess, and replies in the same email thread.

Usage:
    ./bridge.py start   # Start daemon (background)
    ./bridge.py stop    # Stop daemon
    ./bridge.py run     # Run in foreground (for debugging)
"""

import base64
import email.utils
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
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
LOG_FILE = CONFIG_DIR / "bridge.log"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

POLL_INTERVAL = 30  # seconds
CLAUDE_TIMEOUT = 600  # 10 minutes
MAX_RESPONSE_LEN = 50_000  # chars
CLAUDE_CWD = "/Users/zhengli.sun/Projects"
SUBJECT_PREFIX = "[claude]"
REPLY_SENDER_NAME = "ClaudeRemote"  # Display name on reply emails
ATTACHMENTS_DIR = CONFIG_DIR / "attachments"
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB
ATTACHMENT_MAX_AGE_HOURS = 24

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
            parts.append(f"[{role} — {msg['date']}]\n{body}")
    return "\n\n---\n\n".join(parts)


def send_reply(service, original_msg: dict, body_text: str, my_email: str):
    """Reply to a message in the same thread with a distinct sender name."""
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

    message = MIMEText(body_text)
    message["from"] = f"{REPLY_SENDER_NAME} <{my_email}>"
    message["to"] = original_msg["from"]
    message["subject"] = subject
    message["In-Reply-To"] = original_msg.get("message_id", "")
    message["References"] = references

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

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


def invoke_claude(message: str, session_id: str, resume: bool = False) -> str:
    """Run claude -p as a subprocess and return the output."""
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
            cwd=CLAUDE_CWD,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            output = f"[Claude exited with code {result.returncode}]\n{result.stderr.strip()}"
        if not output:
            output = "[Claude returned empty output]"
    except subprocess.TimeoutExpired:
        output = f"[Claude timed out after {CLAUDE_TIMEOUT}s]"
        log.warning("Claude timed out for session %s", session_id[:8])
    except FileNotFoundError:
        output = "[Error: 'claude' command not found. Is Claude Code installed?]"
        log.error("claude command not found")

    # Truncate if too large
    if len(output) > MAX_RESPONSE_LEN:
        output = output[:MAX_RESPONSE_LEN] + "\n\n[truncated — response exceeded 50K chars]"

    return output


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
    """Load thread→session mapping from JSON file."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_thread_sessions(sessions: dict[str, str]):
    """Save thread→session mapping to JSON file."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


# ---------------------------------------------------------------------------
# Main Polling Loop
# ---------------------------------------------------------------------------


def run_bridge(foreground: bool = False):
    """Main loop: poll Gmail, process messages, reply."""
    setup_logging(foreground=foreground)
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

    # Record startup time — ignore messages older than this
    startup_time_ms = int(time.time() * 1000)
    log.info("Startup time: %s (ignoring older messages)", datetime.now(timezone.utc).isoformat())
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
            _poll_cycle(service, my_email, processed_ids, thread_sessions, startup_time_ms)
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

    query = f"subject:{SUBJECT_PREFIX} is:unread"
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

        # Safety: ignore messages from before startup
        if msg["internal_date_ms"] < startup_time_ms:
            log.info("Skipping pre-startup message %s (date=%s)", msg_id, msg["date"])
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

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

        # Extract the user's question
        body = msg["body"].strip()
        if not body and not attachment_paths:
            log.info("Skipping message %s with empty body and no attachments", msg_id)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        thread_id = msg["thread_id"]
        log.info("Processing message %s in thread %s: %.80s", msg_id, thread_id, body)

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

        # Invoke Claude
        response = invoke_claude(prompt, session_id, resume=resume)

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
            response = invoke_claude(prompt, session_id, resume=False)

        # Reply in thread
        try:
            send_reply(service, msg, response, my_email)
            log.info("Replied to message %s (session=%s)", msg_id, session_id[:8])
        except Exception:
            log.exception("Failed to send reply for message %s", msg_id)

        # Update state
        mark_as_read(service, msg_id)
        processed_ids.add(msg_id)
        save_processed_id(msg_id)
        thread_sessions[thread_id] = session_id
        save_thread_sessions(thread_sessions)


# ---------------------------------------------------------------------------
# Daemon Management
# ---------------------------------------------------------------------------


def start_daemon():
    """Start the bridge as a background process."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"Bridge already running (PID {pid})")
            return
        except OSError:
            PID_FILE.unlink()  # Stale PID file

    # Fork into background
    pid = os.fork()
    if pid > 0:
        # Parent
        print(f"Bridge started (PID {pid})")
        print(f"Logs: {LOG_FILE}")
        return

    # Child: become session leader
    os.setsid()

    # Second fork to fully detach
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect stdio
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    try:
        run_bridge(foreground=False)
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


def stop_daemon():
    """Stop the running bridge daemon."""
    if not PID_FILE.exists():
        print("Bridge is not running (no PID file)")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to bridge (PID {pid})")
        # Wait briefly for clean shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        print("Bridge stopped")
    except OSError:
        print(f"Process {pid} not found (stale PID file)")
    finally:
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
