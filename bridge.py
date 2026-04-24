#!/usr/bin/env python3
"""ClaudeRemote: unified Gmail + Slack remote interface for Claude Code.

Polls Gmail for emails with "cc" subject prefix and/or Slack via MCP for
new messages, feeds them to Claude Code via subprocess, and replies in the
same email thread or Slack thread.

Usage:
    ./bridge.py run [--gmail] [--slack] [--all]   # Run in foreground
    ./bridge.py start [--gmail] [--slack] [--all]  # Start daemon
    ./bridge.py stop                                # Stop daemon
    ./bridge.py status                              # Show status
"""
from __future__ import annotations

import argparse
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
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".claude-remote"
_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Parse KEY=VALUE pairs into os.environ without overwriting existing vars."""
    if not path.is_file():
        return
    with open(path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            if _line.startswith("export "):
                _line = _line[7:]
            _key, _, _val = _line.partition("=")
            _val = _val.strip('"').strip("'").replace("$HOME", str(Path.home()))
            os.environ.setdefault(_key.strip(), _val)


# Load env overrides. Project .env wins by being loaded first (setdefault skips
# already-set keys), then ~/.claude-remote/env fills in anything still missing.
_load_env_file(_SCRIPT_DIR / ".env")
_load_env_file(CONFIG_DIR / "env")

CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
GMAIL_PROCESSED_FILE = CONFIG_DIR / "processed.txt"
SESSIONS_FILE = CONFIG_DIR / "thread_sessions.json"
PID_FILE = CONFIG_DIR / "bridge.pid"
LOCK_FILE = CONFIG_DIR / "bridge.lock"
LOG_FILE = CONFIG_DIR / "bridge.log"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

POLL_INTERVAL = int(os.environ.get("CLAUDE_REMOTE_POLL_INTERVAL", "5"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_REMOTE_TIMEOUT", "3600"))  # seconds (default 60 min)
CLAUDE_SOFT_CAP_BUFFER = int(os.environ.get("CLAUDE_REMOTE_SOFT_CAP_BUFFER", "600"))  # soft cap = timeout - buffer (default 10 min before)
MAX_RESPONSE_LEN = 50_000  # chars
JOURNAL_DIR = Path(os.environ.get("CLAUDE_REMOTE_JOURNAL_DIR", str(Path.home() / "projects" / "journal")))
CLAUDE_CWD = os.environ.get("CLAUDE_REMOTE_CWD", str(Path.home() / "Projects"))
SUBJECT_PREFIX = "cc"
REPLY_SENDER_NAME = "ClaudeRemote"  # Display name on reply emails
ATTACHMENTS_DIR = CONFIG_DIR / "attachments"
AUDIO_CACHE_DIR = CONFIG_DIR / "audio_cache"
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB
ATTACHMENT_MAX_AGE_HOURS = 24
WHISPER_MODEL = os.environ.get("CLAUDE_REMOTE_WHISPER_MODEL", "base")
PROGRESS_INTERVAL = 120  # seconds between "still working" emails
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"

HELP_TEXT = """\
Available commands and capabilities:

/help -- Show this help message
/sessions -- List recent Claude Code sessions
/resume <session-id> -- Resume a specific session
/cancel -- Cancel a running task (coming soon)
/daily-summary -- Generate and send a work summary for today
/daily-brief -- Morning briefing with TODOs, calendar, PRs, email, Slack

Regular messages -- Sent to Claude Code for processing
Attachments -- Attach files to emails and Claude will analyze them
Calendar, email, docs -- Claude has access to Google Workspace tools
Multi-turn -- Reply in the same thread to continue a conversation"""

DIGEST_ENABLED = os.environ.get("CLAUDE_REMOTE_DIGEST_ENABLED", "false").lower() == "true"
DIGEST_HOUR = int(os.environ.get("CLAUDE_REMOTE_DIGEST_HOUR", "8"))
DIGEST_LAST_SENT_FILE = CONFIG_DIR / "digest_last_sent.txt"

CANCEL_FILE = CONFIG_DIR / "cancel.txt"

RATE_LIMIT_PER_HOUR = int(os.environ.get("CLAUDE_REMOTE_RATE_LIMIT_PER_HOUR", "200"))
RATE_LIMIT_FILE = CONFIG_DIR / "rate_limit.json"  # Tracks invocation timestamps

SUMMARY_ENABLED = os.environ.get("CLAUDE_REMOTE_SUMMARY_ENABLED", "false").lower() == "true"
SUMMARY_HOUR = int(os.environ.get("CLAUDE_REMOTE_SUMMARY_HOUR", "22"))
SUMMARY_LAST_SENT_FILE = CONFIG_DIR / "summary_last_sent.txt"

# Slack MCP
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
SLACK_STATE_FILE = CONFIG_DIR / "slack_agent_state.json"
MCP_URL = "https://mcp.slack.com/mcp"
AGENT_PREFIX = ":robot_face:"
BUSINESS_HOURS_START = int(os.environ.get("CLAUDE_REMOTE_BIZ_START", "8"))
BUSINESS_HOURS_END = int(os.environ.get("CLAUDE_REMOTE_BIZ_END", "22"))
BUSINESS_HOURS_ONLY = os.environ.get("CLAUDE_REMOTE_BIZ_ONLY", "false").lower() == "true"
SLACK_CHANNEL_NAME = os.environ.get("CLAUDE_REMOTE_SLACK_CHANNEL", "your-agent-channel")
SLACK_USER_ID = os.environ.get("CLAUDE_REMOTE_SLACK_USER_ID", "")  # auto-resolved at startup if empty
SLACK_NOTIFY_THRESHOLD = int(os.environ.get("CLAUDE_REMOTE_NOTIFY_THRESHOLD", "30"))  # seconds

# Optional bot token (xoxb-*). When set, outbound writes (chat.postMessage,
# reactions.add, error DMs) go through the bot so pings come from the bot
# instead of the user's own account (which suppresses self-notifications).
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()

# Cross-channel invocation via @ClaudeRemote keyword search
CROSS_CHANNEL_ENABLED = os.environ.get("CLAUDE_REMOTE_CROSS_CHANNEL", "true").lower() == "true"
# Keyword trigger. Set to empty string to disable and rely solely on real
# @-mentions of the bot (halves cross-channel search load under rate limits).
CROSS_CHANNEL_TRIGGER = os.environ.get("CLAUDE_REMOTE_TRIGGER", "@ClaudeRemote")
# When a bot token is configured, also treat real @-mentions of the bot as a
# trigger. Slack renders @MXADXP in messages as <@{bot_uid}>. Set to "false" to
# disable and only match the keyword trigger above.
BOT_MENTION_ENABLED = os.environ.get("CLAUDE_REMOTE_BOT_MENTION", "true").lower() == "true"
# Turn off polling of the private agent channel (#al-claude-remote) entirely,
# so the only outbound Slack load is the cross-channel mention search. Useful
# once the bot is installed in channels you care about and you interact via
# @-mention or DM exclusively.
DISABLE_PRIVATE_POLL = os.environ.get("CLAUDE_REMOTE_DISABLE_PRIVATE_POLL", "false").lower() == "true"

# Messages we have already warned about being rate-limited. Re-attempted each
# poll cycle (no re-log) until the hourly budget frees up and they land.
_DEFERRED_MENTIONS: set = set()
# Comma-separated list of user IDs allowed to trigger via @ClaudeRemote (in addition to SLACK_USER_ID)
_extra_users = os.environ.get("CLAUDE_REMOTE_ALLOWED_USERS", "")
CROSS_CHANNEL_ALLOWED_USERS: set[str] = {
    uid.strip() for uid in _extra_users.split(",") if uid.strip()
}
# Comma-separated channel IDs where ANY user can trigger @ClaudeRemote
_open_channels = os.environ.get("CLAUDE_REMOTE_OPEN_CHANNELS", "")
CROSS_CHANNEL_OPEN_CHANNELS: set[str] = {
    cid.strip() for cid in _open_channels.split(",") if cid.strip()
}

# Safety guardrails - prepended to every non-resume Claude invocation
SAFETY_PREAMBLE = """\
SAFETY RULES (MANDATORY - violations are not acceptable):
1. NO PII EXPOSURE: Never output personal data (emails, phone numbers, addresses, \
SSNs, financial info) in Slack messages or threads. Redact or summarize instead.
2. NO SECRET LEAKS: Never output API keys, tokens, passwords, credentials, private \
keys, or .env file contents. If you find them during research, do not include them \
in your response.
3. NO PRODUCTION MUTATIONS: Do not run commands or make API calls that modify \
production systems, databases, or infrastructure. Read-only operations only. \
No deploys, no migrations, no config changes to prod.
4. NO PR APPROVALS: Do not approve, merge, or submit PR reviews unless the user \
explicitly says "approve this PR" or "merge this PR" in the current message.
5. NO DESTRUCTIVE GIT OPS: Do not force-push, delete branches, reset --hard, or \
amend published commits.
6. NO SENDING EMAILS/MESSAGES ON BEHALF OF USER: Do not use Gmail send or Slack \
send tools to send messages as the user unless the current message explicitly asks \
you to send/post something.
7. NO FILE DELETION: Do not delete files, folders, or resources unless explicitly asked.
8. SCOPE LIMITATION: Only perform the task described in the current message. Do not \
take additional actions "while you're at it" or make unsolicited changes.
"""

# Concurrency
MAX_CONCURRENT_INVOCATIONS = int(os.environ.get("CLAUDE_REMOTE_MAX_CONCURRENT", "3"))
_executor: Optional[ThreadPoolExecutor] = None
_state_lock = threading.Lock()
_inflight: set = set()
_inflight_lock = threading.Lock()

# Module-level state (set in run_bridge)
_startup_time: Optional[datetime] = None
_messages_processed: int = 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("claude-remote")


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every emit."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging(foreground: bool = False):
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
# Shared Utilities
# ---------------------------------------------------------------------------


def is_business_hours() -> bool:
    """Check if we are within business hours (for Slack gating)."""
    if not BUSINESS_HOURS_ONLY:
        return True
    hour = datetime.now().hour
    return BUSINESS_HOURS_START <= hour < BUSINESS_HOURS_END


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


def search_messages(service, query: str) -> list:
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


def _extract_attachments(payload: dict) -> list:
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


def download_attachments(service, msg_id: str, attachment_metas: list) -> list:
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
    for cache_dir in (ATTACHMENTS_DIR, AUDIO_CACHE_DIR):
        if not cache_dir.exists():
            continue
        cutoff = time.time() - ATTACHMENT_MAX_AGE_HOURS * 3600
        for child in cache_dir.iterdir():
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slack Audio Transcription
# ---------------------------------------------------------------------------

# Regex to match Slack file references in MCP-formatted messages
_AUDIO_FILE_RE = re.compile(
    r"Files?:\s*(.+?)\s*\(ID:\s*(\w+),\s*(audio/\S+),\s*([\d.]+\s*\w+)\)",
)

_whisper_model_cache = None


def _get_whisper_model():
    """Lazy-load the whisper model (downloads on first use)."""
    global _whisper_model_cache
    if _whisper_model_cache is not None:
        return _whisper_model_cache
    try:
        import whisper  # type: ignore
        log.info("Loading whisper model '%s'...", WHISPER_MODEL)
        _whisper_model_cache = whisper.load_model(WHISPER_MODEL)
        log.info("Whisper model loaded")
        return _whisper_model_cache
    except ImportError:
        log.warning("openai-whisper not installed, audio transcription disabled")
        return None
    except Exception as exc:
        log.warning("Failed to load whisper model: %s", exc)
        return None


def _get_slack_file_token() -> tuple:
    """Get a valid xoxc token and d cookie for downloading Slack files.

    Reads from the Slack desktop app's local storage (LevelDB) and cookie
    database. Returns (xoxc_token, d_cookie) or (None, None) on failure.
    """
    leveldb_path = Path.home() / "Library" / "Application Support" / "Slack" / "Local Storage" / "leveldb"
    cookies_path = Path.home() / "Library" / "Application Support" / "Slack" / "Cookies"

    if not leveldb_path.exists() or not cookies_path.exists():
        return None, None

    # Find xoxc token for the DoorDash enterprise workspace from localStorage
    xoxc_token = None
    try:
        import sqlite3
        import hashlib

        # Read all xoxc tokens from LevelDB files and pick the enterprise one
        # by looking for context clues (the enterprise domain entry nearby)
        best_token = None
        for fname in sorted(leveldb_path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not fname.suffix in ('.ldb', '.log'):
                continue
            try:
                data = fname.read_bytes()
                # Look for enterprise Slack token (associated with enterprise workspace)
                # Try localConfig_v2 pattern first
                for prefix in (b'"doordash.enterprise', b'"doordash"', b'"domain":"doordash"'):
                    if prefix in data:
                        idx = 0
                        while True:
                            idx = data.find(b'xoxc-', idx)
                            if idx < 0:
                                break
                            end = idx
                            while end < len(data) and end - idx < 300:
                                if data[end:end+1] in (b'"', b"'", b'\n', b'\x00', b'\x01'):
                                    break
                                end += 1
                            candidate = data[idx:end].decode(errors='ignore')
                            if len(candidate) > 50:
                                best_token = candidate
                            idx = end
                        if best_token:
                            break
                if best_token:
                    break
            except Exception:
                continue
        xoxc_token = best_token

        # Decrypt the d cookie from the Cookies database
        key_bytes = subprocess.check_output(
            ['security', 'find-generic-password', '-s', 'Slack Safe Storage', '-w'],
            stderr=subprocess.DEVNULL,
        ).strip()
        derived_key = hashlib.pbkdf2_hmac('sha1', key_bytes, b'saltysalt', 1003, dklen=16)

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        conn = sqlite3.connect(str(cookies_path))
        cursor = conn.cursor()
        cursor.execute("SELECT encrypted_value FROM cookies WHERE host_key LIKE '%slack.com%' AND name = 'd'")
        row = cursor.fetchone()
        conn.close()

        d_cookie = None
        if row:
            enc_val = row[0]
            if enc_val[:3] == b'v10':
                iv = b' ' * 16
                cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv), backend=default_backend())
                decryptor = cipher.decryptor()
                decrypted = decryptor.update(enc_val[3:]) + decryptor.finalize()
                pad_len = decrypted[-1] if isinstance(decrypted[-1], int) else ord(decrypted[-1])
                dec_str = decrypted[:-pad_len].decode('utf-8', errors='replace')
                xoxd_idx = dec_str.find('xoxd-')
                if xoxd_idx >= 0:
                    d_cookie = dec_str[xoxd_idx:]

        return xoxc_token, d_cookie

    except Exception as exc:
        log.debug("Failed to extract Slack file token: %s", exc)
        return None, None


def _get_slack_file_token_from_browser() -> tuple:
    """Fallback: get the token from the Slack web app's localStorage via Playwright.

    This is used when the desktop app tokens are stale. Requires an active
    browser session with Slack cookies.
    """
    # For now, return None. The desktop token path covers the common case.
    return None, None


_slack_file_token_cache = None


def _cached_slack_file_token() -> tuple:
    """Return cached (xoxc, d_cookie) pair, refreshing once per session."""
    global _slack_file_token_cache
    if _slack_file_token_cache is None:
        _slack_file_token_cache = _get_slack_file_token()
    return _slack_file_token_cache


def download_slack_audio(file_id: str, filename: str) -> Optional[Path]:
    """Download a Slack audio file by file ID. Returns local path or None."""
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    dest = AUDIO_CACHE_DIR / f"{file_id}_{filename}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    xoxc, d_cookie = _cached_slack_file_token()
    if not xoxc or not d_cookie:
        log.warning("No Slack file credentials available, cannot download audio")
        return None

    # Step 1: get the private download URL via files.info
    try:
        form_data = urlencode({"file": file_id}).encode()
        req = URLRequest(
            "https://doordashext.slack.com/api/files.info",
            data=form_data,
            headers={
                "Authorization": f"Bearer {xoxc}",
                "Cookie": f"d={d_cookie}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read())

        if not info.get("ok"):
            log.warning("files.info failed for %s: %s", file_id, info.get("error"))
            return None

        download_url = info["file"].get("url_private_download")
        if not download_url:
            log.warning("No download URL for file %s", file_id)
            return None
    except Exception as exc:
        log.warning("Failed to get file info for %s: %s", file_id, exc)
        return None

    # Step 2: download the file
    try:
        req = URLRequest(
            download_url,
            headers={
                "Authorization": f"Bearer {xoxc}",
                "Cookie": f"d={d_cookie}",
            },
        )
        with urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        log.info("Downloaded audio %s (%d bytes)", file_id, dest.stat().st_size)
        return dest
    except Exception as exc:
        log.warning("Failed to download audio %s: %s", file_id, exc)
        if dest.exists():
            dest.unlink()
        return None


def transcribe_audio(audio_path: Path) -> Optional[str]:
    """Transcribe an audio file using whisper. Returns text or None."""
    model = _get_whisper_model()
    if model is None:
        return None

    try:
        result = model.transcribe(str(audio_path), language="en")
        text = result.get("text", "").strip()
        if text:
            log.info("Transcribed %s: %.80s", audio_path.name, text)
        return text or None
    except Exception as exc:
        log.warning("Transcription failed for %s: %s", audio_path.name, exc)
        return None


def process_slack_audio_files(message_text: str) -> str:
    """Detect audio file references in a Slack message, download and transcribe them.

    Returns a preamble string with transcriptions to prepend to the prompt,
    or an empty string if no audio files were found or transcription failed.
    """
    matches = _AUDIO_FILE_RE.findall(message_text)
    if not matches:
        return ""

    transcriptions = []
    for filename, file_id, mime_type, size_str in matches:
        if not mime_type.startswith("audio/"):
            continue
        log.info("Processing audio file: %s (ID: %s)", filename.strip(), file_id)

        audio_path = download_slack_audio(file_id, filename.strip().replace(" ", "_"))
        if audio_path is None:
            transcriptions.append(f"[Audio: {filename.strip()} could not be downloaded]")
            continue

        text = transcribe_audio(audio_path)
        if text is None:
            transcriptions.append(f"[Audio: {filename.strip()} could not be transcribed]")
            continue

        transcriptions.append(f'Audio transcription of "{filename.strip()}":\n{text}')

    if not transcriptions:
        return ""

    return "\n\n".join(transcriptions) + "\n\n"


def get_thread_history(service, thread_id: str) -> list:
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


def build_thread_context(thread_messages: list) -> str:
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


def _parse_json_output(raw: str) -> str:
    """Extract the result text from claude --output-format json."""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        result = data.get("result", "")
        turns = data.get("num_turns", "?")
        cost = data.get("total_cost_usd")
        cost_str = f"${cost:.2f}" if cost else "?"
        log.info("Claude JSON output: %d chars, %s turns, cost %s", len(result), turns, cost_str)
        return result.strip() if result else ""
    except (json.JSONDecodeError, TypeError):
        # If JSON parsing fails, fall back to raw text
        log.warning("Failed to parse Claude JSON output (%d chars), using raw", len(raw))
        return raw


def _create_timeout_journal(session_id: str, message: str, elapsed: int) -> Optional[str]:
    """Create a journal doc for timed-out tasks. Returns the file path or None."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        slug = session_id[:8]
        filename = f"{today}-claude-remote-timeout-{slug}.md"
        doc_path = JOURNAL_DIR / "docs" / filename

        # Truncate the original message for the doc
        msg_preview = message[:2000] + ("..." if len(message) > 2000 else "")

        content = f"""# Claude Remote Timeout Report

**Session:** `{session_id}`
**Date:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**Elapsed:** {elapsed // 60} minutes {elapsed % 60} seconds
**Timeout:** {CLAUDE_TIMEOUT // 60} minutes (soft cap at {(CLAUDE_TIMEOUT - CLAUDE_SOFT_CAP_BUFFER) // 60} min)

## Original Request

{msg_preview}

## Status

This task hit the time limit before Claude could produce a final summary.
The session (`{session_id}`) can be resumed with `/resume` in the Slack thread.

## What to do

1. Reply in the Slack thread to continue where it left off
2. Break the task into smaller steps
3. Check if partial work was completed (PRs, branches, files)
"""

        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(content)
        log.info("Created timeout journal doc: %s", doc_path)

        # Try to commit and push to journal repo
        journal_root = JOURNAL_DIR
        try:
            subprocess.run(
                ["git", "add", str(doc_path)],
                cwd=str(journal_root), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Claude Remote timeout report ({slug})"],
                cwd=str(journal_root), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "push"],
                cwd=str(journal_root), capture_output=True, timeout=30,
            )
            log.info("Pushed timeout journal doc to git")
        except Exception as e:
            log.warning("Failed to git push journal doc: %s", e)

        return str(doc_path)
    except Exception as e:
        log.error("Failed to create timeout journal doc: %s", e)
        return None


def _soft_cap_summarize(proc, session_id: str, elapsed: int, message: str) -> str:
    """Send SIGINT for graceful stop, then resume to ask for a summary."""
    log.warning(
        "Soft cap reached at %d min for session %s, sending SIGINT",
        elapsed // 60, session_id[:8],
    )
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        pass

    # Wait up to 30s for graceful exit
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        log.warning("SIGINT didn't stop Claude in 30s, killing")
        proc.kill()
        proc.wait()

    # Read whatever output was produced before the interrupt
    partial_output = proc.stdout.read().strip()
    partial_result = _parse_json_output(partial_output) if partial_output else ""

    # Resume the session to ask for a summary
    log.info("Resuming session %s for soft-cap summary", session_id[:8])
    summary_prompt = (
        "You were interrupted because you're approaching the time limit. "
        "STOP all tool use. Summarize what you've accomplished so far, "
        "what remains to be done, and any findings or partial results. "
        "Be concise but thorough."
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    summary_cmd = [
        "claude", "-p", "--output-format", "json", "--dangerously-skip-permissions",
        "--resume", session_id, summary_prompt,
    ]

    try:
        summary_proc = subprocess.run(
            summary_cmd, capture_output=True, text=True,
            env=env, cwd=CLAUDE_CWD, timeout=300,  # 5 min max for summary
        )
        summary_text = _parse_json_output(summary_proc.stdout)
    except (subprocess.TimeoutExpired, Exception) as e:
        log.warning("Summary resume failed: %s", e)
        summary_text = ""

    # Create journal doc
    journal_path = _create_timeout_journal(session_id, message, elapsed)

    # If we got a summary, append it to the journal doc
    if summary_text and journal_path:
        try:
            with open(journal_path, "a") as f:
                f.write(f"\n## Claude's Summary (auto-generated at soft cap)\n\n{summary_text}\n")
            # Re-commit with the summary
            journal_root = JOURNAL_DIR
            subprocess.run(
                ["git", "add", journal_path],
                cwd=str(journal_root), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Update timeout report with Claude summary ({session_id[:8]})"],
                cwd=str(journal_root), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "push"],
                cwd=str(journal_root), capture_output=True, timeout=30,
            )
        except Exception as e:
            log.warning("Failed to update journal doc with summary: %s", e)

    # Build the final output
    parts = []
    if summary_text:
        parts.append(summary_text)
    elif partial_result:
        parts.append(partial_result)

    parts.append(
        f"\n\n:hourglass: *Hit the soft time cap ({elapsed // 60} min).* "
        f"Session `{session_id[:8]}` can be resumed."
    )

    if journal_path:
        # Convert to relative path for display
        rel_path = journal_path.replace(str(Path.home()), "~")
        parts.append(f"\n:notebook: Full report saved to `{rel_path}`")

    parts.append("\nReply in this thread to continue, or `/resume` to pick up the session.")

    return "\n".join(parts)


def invoke_claude(
    message: str,
    session_id: Optional[str] = None,
    resume: bool = False,
    on_progress: Optional[object] = None,
    thread_id: Optional[str] = None,
) -> str:
    """Run claude -p as a subprocess, sending progress callbacks while waiting.

    Uses JSON output format to capture results even when Claude spends all its
    time on tool calls. Implements a soft cap (SIGINT + summary) before the
    hard timeout (SIGKILL).
    """
    if session_id is None:
        session_id = str(uuid.uuid4())

    # Inject safety guardrails on new sessions (resumed sessions already have them)
    if not resume:
        message = SAFETY_PREAMBLE + "\n" + message

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Strip nested session guard

    cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    if resume:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])
    cmd.append(message)

    soft_cap = CLAUDE_TIMEOUT - CLAUDE_SOFT_CAP_BUFFER
    log.info(
        "Invoking Claude (resume=%s, session=%s, timeout=%dmin, soft_cap=%dmin)",
        resume, session_id[:8], CLAUDE_TIMEOUT // 60, soft_cap // 60,
    )

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=CLAUDE_CWD,
        )
        elapsed = 0
        next_progress = PROGRESS_INTERVAL
        next_log = 60  # Log every 60s that Claude is still running
        while proc.poll() is None:
            time.sleep(1)
            elapsed += 1

            # Periodic activity logging
            if elapsed >= next_log:
                log.info(
                    "Claude still running (session=%s, elapsed=%dm%ds, pid=%d)",
                    session_id[:8], elapsed // 60, elapsed % 60, proc.pid,
                )
                next_log += 60

            if thread_id and _check_cancel(thread_id):
                log.info("Cancelling Claude for thread %s (elapsed=%ds)", thread_id, elapsed)
                proc.kill()
                proc.wait()
                return "[Cancelled by user]\n\nThe task was cancelled. Send a new message to start fresh."

            # Soft cap: graceful stop + summary
            if elapsed >= soft_cap:
                log.warning(
                    "Soft cap reached for session %s at %dm (hard cap at %dm)",
                    session_id[:8], elapsed // 60, CLAUDE_TIMEOUT // 60,
                )
                output = _soft_cap_summarize(proc, session_id, elapsed, message)
                break

            if on_progress and elapsed >= next_progress:
                on_progress(elapsed)
                next_progress += PROGRESS_INTERVAL
        else:
            raw_output = proc.stdout.read().strip()
            output = _parse_json_output(raw_output)

            if proc.returncode != 0 and not output:
                stderr_text = proc.stderr.read().strip()
                output = (
                    f"[Claude exited with code {proc.returncode}]\n\n"
                    + (f"Error: {stderr_text}\n\n" if stderr_text else "")
                    + "Reply in this thread to retry, or start a new thread with cc prefix."
                )
                log.error("Claude exited with code %d (session=%s)", proc.returncode, session_id[:8])
            elif not output:
                # Even with JSON format, output can be empty if Claude produced no text
                log.warning(
                    "Claude returned empty result (session=%s, returncode=%d, raw_len=%d)",
                    session_id[:8], proc.returncode, len(raw_output),
                )
                output = (
                    "[Claude returned empty output]\n\n"
                    "Claude completed but produced no text response (all work was via tool calls). "
                    "Reply in this thread to ask for a summary of what was done, "
                    "or check for any PRs/branches that were created."
                )
            else:
                log.info(
                    "Claude completed (session=%s, elapsed=%ds, output=%d chars)",
                    session_id[:8], elapsed, len(output),
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


def delete_claude_session(session_id: str):
    """Delete a Claude Code session file from disk."""
    cwd_slug = CLAUDE_CWD.replace("/", "-").replace(".", "-")
    session_file = CLAUDE_SESSIONS_DIR / cwd_slug / f"{session_id}.jsonl"
    try:
        session_file.unlink()
        log.info("Deleted Claude session file: %s", session_id[:8])
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("Failed to delete session file %s: %s", session_id[:8], e)


# ---------------------------------------------------------------------------
# Gmail State Management
# ---------------------------------------------------------------------------


def load_processed_ids() -> set:
    """Load processed message IDs from file."""
    if not GMAIL_PROCESSED_FILE.exists():
        return set()
    return set(GMAIL_PROCESSED_FILE.read_text().strip().splitlines())


def save_processed_id(msg_id: str):
    """Append a processed message ID to file."""
    with open(GMAIL_PROCESSED_FILE, "a") as f:
        f.write(msg_id + "\n")


def load_thread_sessions() -> dict:
    """Load thread->session mapping from JSON file."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_thread_sessions(sessions: dict):
    """Save thread->session mapping to JSON file."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def _check_rate_limit() -> tuple:
    """Check if we're within the rate limit. Returns (allowed, remaining). Thread-safe."""
    with _state_lock:
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
    """Record a Claude invocation timestamp for rate limiting. Thread-safe."""
    with _state_lock:
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
# Inflight Tracking
# ---------------------------------------------------------------------------


def _is_inflight(ts: str) -> bool:
    with _inflight_lock:
        return ts in _inflight


def _mark_inflight(ts: str):
    with _inflight_lock:
        _inflight.add(ts)


def _unmark_inflight(ts: str):
    with _inflight_lock:
        _inflight.discard(ts)


# ---------------------------------------------------------------------------
# Async Invoke and Reply
# ---------------------------------------------------------------------------


def _async_invoke_and_reply(
    token: str,
    channel_id: str,
    thread_ts: str,
    msg_ts: str,
    prompt: str,
    session_id: str,
    resume: bool,
    on_success,
    make_retry_prompt=None,
    notify_user_id: str = "",
):
    """Process a Claude invocation in a background thread.

    on_success(state, session_id) is called inside _state_lock to atomically
    update state after a successful reply.
    notify_user_id: Slack user ID to @mention in the reply for slow tasks.
    """
    global _messages_processed
    try:
        _record_invocation()
        start_time = time.time()
        response = invoke_claude(prompt, session_id, resume=resume, thread_id=thread_ts)

        # Retry fresh if resume failed
        if resume and "[Claude exited with code" in response and make_retry_prompt:
            log.warning("Resume failed for session %s, retrying fresh", session_id[:8])
            session_id = str(uuid.uuid4())
            response = invoke_claude(make_retry_prompt(), session_id, resume=False, thread_id=thread_ts)

        elapsed = time.time() - start_time

        # Build reply with optional @mention for slow tasks
        mention_id = notify_user_id or SLACK_USER_ID
        if mention_id and elapsed >= SLACK_NOTIFY_THRESHOLD:
            reply_text = f"{AGENT_PREFIX} <@{mention_id}> {response}"
        else:
            reply_text = f"{AGENT_PREFIX} {response}"

        success = mcp_send_message(token, channel_id, thread_ts, reply_text, notify_user_id=notify_user_id)
        if not success and len(reply_text) > 4000:
            # Retry with truncated message (Slack has ~4000 char limit per block)
            log.warning("Reply too long (%d chars), retrying truncated", len(reply_text))
            truncated = reply_text[:3900] + "\n\n_(truncated, response was too long for Slack)_"
            success = mcp_send_message(token, channel_id, thread_ts, truncated, notify_user_id=notify_user_id)
        if not success and notify_user_id:
            # Fallback: DM the requester (handles Slack Connect channels etc.)
            log.warning("Thread reply failed, falling back to DM for %s", notify_user_id)
            dm_text = (
                f"{AGENT_PREFIX} _(Reply to your `{CROSS_CHANNEL_TRIGGER}` request in <#{channel_id}> - "
                f"couldn't post there directly)_\n\n{response}"
            )
            if len(dm_text) > 4000:
                dm_text = dm_text[:3900] + "\n\n_(truncated)_"
            success = mcp_send_message(token, notify_user_id, "", dm_text, notify_user_id=notify_user_id)
            if success:
                log.info("Sent DM fallback to %s", notify_user_id)
        if success:
            log.info("Replied in %s thread %s (session=%s, %.1fs)", channel_id, thread_ts, session_id[:8], elapsed)
            mcp_add_reaction(token, channel_id, msg_ts, "white_check_mark")
            with _state_lock:
                state = load_slack_state()
                on_success(state, session_id)
                save_slack_state(state)
        else:
            log.error("Failed to reply in %s thread %s (reply_len=%d)", channel_id, thread_ts, len(reply_text))

        _messages_processed += 1
    except Exception:
        log.exception("Error in async invocation (session=%s)", session_id[:8])
    finally:
        _unmark_inflight(msg_ts)


# ---------------------------------------------------------------------------
# Daily Digest
# ---------------------------------------------------------------------------


CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _skill_exists(skill_name: str) -> bool:
    """Check if a Claude Code skill is installed."""
    return (CLAUDE_SKILLS_DIR / skill_name).is_dir()


# Built-in prompts for skills that would otherwise require external skill files.
# These are used as fallback when the skill directory doesn't exist.
_BUILTIN_SKILL_PROMPTS = {
    "daily-brief": (
        "Generate a morning briefing. Include:\n"
        "1. My calendar events for today (use Google Calendar tools)\n"
        "2. Unread important emails (use Gmail search)\n"
        "3. Open PRs that need my review (use GitHub tools if available)\n"
        "4. Recent Slack messages needing my attention\n"
        "Format with clear sections. Use markdown. Be concise."
    ),
    "daily-summary": (
        "Generate an end-of-day work summary for today. Include:\n"
        "1. What I accomplished (check my sent emails, Slack messages, merged PRs)\n"
        "2. Meetings I attended (check calendar)\n"
        "3. Open items / carry-forward to tomorrow\n"
        "Format with clear sections. Use markdown. Be concise."
    ),
}


def _invoke_skill(skill_name: str) -> str:
    """Invoke a Claude Code skill, falling back to built-in prompt if not installed."""
    if _skill_exists(skill_name):
        return invoke_claude(f"/{skill_name}", str(uuid.uuid4()), resume=False)
    if skill_name in _BUILTIN_SKILL_PROMPTS:
        return invoke_claude(_BUILTIN_SKILL_PROMPTS[skill_name], str(uuid.uuid4()), resume=False)
    return f"Unknown skill: {skill_name}"


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
    """Send a morning briefing by invoking the /daily-brief skill."""
    now = datetime.now()

    if DIGEST_LAST_SENT_FILE.exists():
        last_sent = DIGEST_LAST_SENT_FILE.read_text().strip()
        if last_sent == now.strftime("%Y-%m-%d"):
            return

    log.info("Generating morning briefing")
    body = _invoke_skill("daily-brief")

    if not body or body.startswith("["):
        log.warning("Morning briefing failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Morning Briefing -- {now.strftime('%b %d')}", body)
    DIGEST_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent morning briefing")


def _maybe_send_digest(service, my_email, thread_sessions, processed_count):
    """Send digest if it is the right hour, not weekend, and not sent today."""
    if not DIGEST_ENABLED:
        return
    now = datetime.now()
    if now.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
        return
    if now.hour != DIGEST_HOUR:
        return
    send_daily_digest(service, my_email, thread_sessions, processed_count)


# ---------------------------------------------------------------------------
# Daily Work Summary
# ---------------------------------------------------------------------------


def send_work_summary(service, my_email: str):
    """Send an end-of-day work summary by invoking the /daily-summary skill."""
    now = datetime.now()

    if SUMMARY_LAST_SENT_FILE.exists():
        last_sent = SUMMARY_LAST_SENT_FILE.read_text().strip()
        # Skip if sent today (date match) or within last 30 minutes (timestamp)
        if last_sent == now.strftime("%Y-%m-%d"):
            return
        try:
            last_ts = datetime.fromisoformat(last_sent)
            if (now - last_ts).total_seconds() < 1800:
                return
        except ValueError:
            pass

    log.info("Generating work summary")
    body = _invoke_skill("daily-summary")

    if not body or body.startswith("["):
        log.warning("Work summary failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Work Summary -- {now.strftime('%b %d')}", body)
    SUMMARY_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent work summary")


def _maybe_send_summary(service, my_email):
    """Send work summary if it is the right hour, not weekend, and not sent today."""
    if not SUMMARY_ENABLED:
        return
    now = datetime.now()
    if now.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
        return
    if now.hour != SUMMARY_HOUR:
        return
    send_work_summary(service, my_email)


# ---------------------------------------------------------------------------
# Gmail Poll Cycle
# ---------------------------------------------------------------------------


def gmail_poll_cycle(
    service,
    my_email: str,
    processed_ids: set,
    thread_sessions: dict,
    startup_time_ms: int,
):
    """Single Gmail poll iteration."""
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
        if body.lower() == "/daily-summary":
            log.info("Manual work summary requested")
            response = _invoke_skill("daily-summary")
            summary_sent = send_reply(service, msg, response, my_email)
            if summary_sent and "id" in summary_sent:
                processed_ids.add(summary_sent["id"])
                save_processed_id(summary_sent["id"])
            # Don't mark auto-summary as sent - manual /daily-summary is on-demand
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/daily-brief":
            log.info("Manual morning brief requested")
            response = _invoke_skill("daily-brief")
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
        log.info("Session for thread %s: session=%s, resume=%s", thread_id, session_id[:8], resume)

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
# Slack MCP Token Management
# ---------------------------------------------------------------------------


def _load_credentials() -> dict:
    """Load the Claude Code credentials file."""
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except (FileNotFoundError, PermissionError) as e:
        log.error("Credentials file not found: %s (%s)", CREDENTIALS_FILE, e)
        return {}


def _find_slack_token_entry(creds: dict) -> Optional[dict]:
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


SLACK_TOKEN_FILE = CONFIG_DIR / "slack_mcp_token.json"
SLACK_MCP_CLIENT_ID = "1601185624273.8899143856786"
# Refresh token when it has less than this many hours left (0 = disabled)
SLACK_TOKEN_REFRESH_HOURS = int(os.environ.get("CLAUDE_REMOTE_SLACK_REFRESH_HOURS", "2"))


def _notify_refresh_failure(entry: dict, error_msg: str):
    """Notify the user when the user's MCP OAuth token refresh fails.

    Prefers the bot DM path (static bot token keeps working even when the
    user's MCP token is broken). Falls back to MCP+channel if no bot token.
    """
    msg = (
        f"{AGENT_PREFIX} :warning: {error_msg}\n"
        f"Run `cd ~/Projects/claude-remote && .venv/bin/python slack_oauth.py` to re-authenticate."
    )
    # Primary: DM via bot (keeps working when user MCP token is dead)
    if _notify_bot_error(error_msg, SLACK_USER_ID):
        log.info("Sent token refresh failure notification via bot DM")
        return
    # Fallback: old path via user token + MCP
    token = entry.get("accessToken")
    if not token:
        return
    try:
        state = load_slack_state()
        channel_id = state.get("channel_id")
        if channel_id:
            _mcp_call("slack_send_message", {"channel_id": channel_id, "message": msg}, token)
            log.info("Sent token refresh failure notification via channel")
    except Exception as e:
        log.warning("Failed to send refresh failure notification: %s", e)


def _refresh_slack_token(entry: dict) -> Optional[dict]:
    """Refresh the Slack OAuth token using the refresh token.

    Returns updated entry on success, None on failure.
    """
    refresh_token = entry.get("refreshToken")
    if not refresh_token:
        log.warning("No refresh token available for Slack token refresh")
        return None

    log.info("Refreshing Slack OAuth token...")
    try:
        data = urlencode({
            "client_id": SLACK_MCP_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode()

        req = URLRequest(
            "https://slack.com/api/oauth.v2.user.access",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        if not result.get("ok"):
            log.error("Slack token refresh failed: %s", result.get("error", result))
            _notify_refresh_failure(entry, f"Slack token refresh failed: {result.get('error', 'unknown')}")
            return None

        new_entry = {
            "serverUrl": entry.get("serverUrl", MCP_URL),
            "accessToken": result["access_token"],
            "refreshToken": result.get("refresh_token", refresh_token),
            "expiresAt": int((time.time() + result.get("expires_in", 43200)) * 1000),
        }

        SLACK_TOKEN_FILE.write_text(json.dumps(new_entry, indent=2))
        SLACK_TOKEN_FILE.chmod(0o600)

        expires_str = datetime.fromtimestamp(new_entry["expiresAt"] / 1000).isoformat()
        log.info("Slack token refreshed successfully, expires at %s", expires_str)
        return new_entry

    except Exception as e:
        log.error("Slack token refresh error: %s", e)
        _notify_refresh_failure(entry, f"Slack token refresh error: {e}")
        return None


def get_slack_token() -> Optional[str]:
    """Get the Slack OAuth access token.

    Checks in order:
    1. Bridge's own token file (~/.claude-remote/slack_mcp_token.json)
    2. Claude Code credentials file (~/.claude/.credentials.json)

    Auto-refreshes if SLACK_TOKEN_REFRESH_HOURS > 0 and token is near expiry.
    """
    # Try bridge's own persistent token first
    entry = None
    try:
        entry = json.loads(SLACK_TOKEN_FILE.read_text())
    except (FileNotFoundError, PermissionError):
        pass

    # Fall back to Claude Code credentials
    if not entry or not entry.get("accessToken"):
        creds = _load_credentials()
        entry = _find_slack_token_entry(creds)

    if not entry:
        log.error("No active Slack MCP token found in credentials")
        return None

    # Check expiry and auto-refresh
    expires_at = entry.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)

    if expires_at:
        remaining_hours = (expires_at - now_ms) / (1000 * 3600)

        if now_ms > expires_at:
            # Token expired — try refresh before giving up
            if SLACK_TOKEN_REFRESH_HOURS > 0:
                refreshed = _refresh_slack_token(entry)
                if refreshed:
                    return refreshed["accessToken"]
            log.warning(
                "Slack token expired at %s. Run: .venv/bin/python slack_oauth.py",
                datetime.fromtimestamp(expires_at / 1000).isoformat(),
            )
            return None

        if SLACK_TOKEN_REFRESH_HOURS > 0 and remaining_hours < SLACK_TOKEN_REFRESH_HOURS:
            log.info("Slack token expires in %.1f hours, refreshing proactively", remaining_hours)
            refreshed = _refresh_slack_token(entry)
            if refreshed:
                return refreshed["accessToken"]
            # If refresh fails, continue with current token

    return entry["accessToken"]


# ---------------------------------------------------------------------------
# Slack MCP API (direct HTTP, no LLM)
# ---------------------------------------------------------------------------


class McpError:
    """Sentinel returned by _mcp_call on failure so callers can inspect the error."""
    def __init__(self, message: str = "", code: int = 0):
        self.message = message
        self.code = code

    def __bool__(self):
        return False  # falsy, so ``if result is None`` and ``if not result`` both work

    @property
    def is_invalid_blocks(self) -> bool:
        return "invalid_blocks" in self.message

    @property
    def is_externally_shared_restricted(self) -> bool:
        return "externally_shared_channel_restricted" in self.message


_MCP_ERROR_GENERIC = McpError("unknown")


_MCP_MAX_RETRIES = 2  # Extra attempts after the initial call (so up to 3 tries total)
_MCP_RETRY_BACKOFF = (2.0, 5.0)  # Seconds to sleep on each retry if no Retry-After


def _mcp_call(tool_name: str, arguments: dict, token: str) -> Optional[dict | McpError]:
    """Call a Slack MCP tool via HTTP JSON-RPC.

    Returns the result dict on success, or an McpError on failure (falsy, so
    existing ``if result is None`` checks still work). On HTTP 429, honors the
    Retry-After header (capped) and retries up to _MCP_MAX_RETRIES times with
    a small backoff. 429s are logged at WARNING, not ERROR, since they are
    expected load-shedding and already retried.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    for attempt in range(_MCP_MAX_RETRIES + 1):
        req = URLRequest(
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
                    err = body["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    code = err.get("code", 0) if isinstance(err, dict) else 0
                    log.error("MCP error: %s", err)
                    return McpError(msg, code)
                return body.get("result", body)
        except HTTPError as e:
            msg = e.read().decode()[:500]
            if e.code == 429 and attempt < _MCP_MAX_RETRIES:
                retry_after = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                try:
                    sleep_s = min(float(retry_after), 15.0) if retry_after else _MCP_RETRY_BACKOFF[attempt]
                except (TypeError, ValueError):
                    sleep_s = _MCP_RETRY_BACKOFF[attempt]
                log.warning("MCP 429 on %s, retry %d/%d after %.1fs", tool_name, attempt + 1, _MCP_MAX_RETRIES, sleep_s)
                time.sleep(sleep_s)
                continue
            log_fn = log.warning if e.code == 429 else log.error
            log_fn("MCP HTTP error %d on %s: %s", e.code, tool_name, msg)
            return McpError(msg, e.code)
        except (URLError, TimeoutError) as e:
            log.error("MCP connection error on %s: %s", tool_name, e)
            return McpError(str(e))
    return McpError("exhausted retries", 429)


def _extract_mcp_text(result: dict) -> Optional[str]:
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


def mcp_read_channel(token: str, channel_id: str, oldest: str = "", limit: int = 50) -> Optional[str]:
    """Read channel messages via MCP."""
    args = {"channel_id": channel_id, "limit": limit}
    if oldest:
        args["oldest"] = oldest
    result = _mcp_call("slack_read_channel", args, token)
    if not result:
        return None
    return _extract_mcp_text(result)


_THREAD_READ_CACHE: dict = {}
_thread_read_lock = threading.Lock()
_THREAD_READ_TTL_SECS = float(os.environ.get("CLAUDE_REMOTE_THREAD_CACHE_SECS", "30"))


def mcp_read_thread(token: str, channel_id: str, message_ts: str,
                    *, use_cache: bool = True) -> Optional[str]:
    """Read thread replies via MCP with a short in-process cache.

    The cache dedupes repeated reads of the same thread across poll cycles and
    across permalink expansion - big help under Slack MCP rate limits. Set
    use_cache=False for cross-channel processing where we want fresh context.
    """
    key = (channel_id, message_ts)
    now = time.time()
    if use_cache:
        with _thread_read_lock:
            cached = _THREAD_READ_CACHE.get(key)
            if cached and (now - cached[0]) < _THREAD_READ_TTL_SECS:
                return cached[1]
    result = _mcp_call("slack_read_thread", {
        "channel_id": channel_id,
        "message_ts": message_ts,
    }, token)
    if not result:
        return None
    text = _extract_mcp_text(result)
    if text is not None:
        with _thread_read_lock:
            _THREAD_READ_CACHE[key] = (now, text)
            # Keep cache bounded
            if len(_THREAD_READ_CACHE) > 500:
                # Drop the 100 oldest entries
                oldest = sorted(_THREAD_READ_CACHE.items(), key=lambda kv: kv[1][0])[:100]
                for k, _ in oldest:
                    _THREAD_READ_CACHE.pop(k, None)
    return text


# Matches Slack permalinks like https://<team>.slack.com/archives/C123/p1712345678901234
_SLACK_PERMALINK_RE = re.compile(
    r"https://[a-z0-9.-]+\.slack\.com/archives/(?P<channel>[CDG][A-Z0-9]+)/p(?P<ts>\d+)"
)


def _permalink_to_channel_ts(url_match) -> Optional[tuple[str, str]]:
    """Turn a permalink regex match into (channel_id, ts) in Slack API format."""
    channel = url_match.group("channel")
    ts_raw = url_match.group("ts")
    # Slack permalinks encode the ts as <seconds><microseconds> with no dot,
    # where <microseconds> is always 6 digits. Older message IDs can vary, so
    # require at least 10 seconds digits + 6 microseconds = 16 chars.
    if len(ts_raw) < 16:
        return None
    return channel, f"{ts_raw[:-6]}.{ts_raw[-6:]}"


def _expand_slack_permalinks(token: str, text: str, *, max_links: int = 3,
                             per_link_chars: int = 500,
                             skip: Optional[set] = None) -> str:
    """Find Slack permalinks in text, fetch each thread, return a capped summary.

    Dedupes per invocation. `skip` can hold (channel, ts) pairs already included
    as top-level thread context, to avoid fetching them again.
    """
    skip = skip or set()
    seen: set = set()
    parts: list[str] = []
    for m in _SLACK_PERMALINK_RE.finditer(text):
        parsed = _permalink_to_channel_ts(m)
        if not parsed:
            continue
        if parsed in seen or parsed in skip:
            continue
        seen.add(parsed)
        if len(parts) >= max_links:
            break
        channel, ts = parsed
        body = mcp_read_thread(token, channel, ts)
        if not body:
            continue
        trimmed = body.strip()
        if len(trimmed) > per_link_chars:
            trimmed = trimmed[:per_link_chars].rstrip() + " ...(truncated)"
        parts.append(f"--- Linked Slack thread ({channel} ts={ts}) ---\n{trimmed}")
    return "\n\n".join(parts)


_SLACK_MAX_MSG_LEN = 4000  # Slack MCP rejects text > ~4000 chars (invalid_blocks)


def _split_message(message: str, limit: int = _SLACK_MAX_MSG_LEN) -> list[str]:
    """Split a long message into chunks no larger than *limit* chars.

    Prefers splitting on newlines to avoid cutting mid-sentence.
    """
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    while message:
        if len(message) <= limit:
            chunks.append(message)
            break
        split_at = message.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(message[:split_at])
        message = message[split_at:].lstrip("\n")
    return chunks


def _sanitize_for_slack(text: str) -> str:
    """Strip markdown constructs that cause Slack invalid_blocks errors.

    Slack's message formatter chokes on certain markdown patterns (tables,
    HTML tags, deeply nested lists, some link/image syntaxes). This function
    strips them down to plain text equivalents.
    """
    # Remove HTML tags (Slack blocks don't support raw HTML)
    text = re.sub(r"<(?![@#!])(/?[a-zA-Z][^>]*)>", "", text)
    # Convert markdown tables to plain text (header + separator + rows)
    text = re.sub(r"^\|(.+)\|$", lambda m: m.group(1).replace("|", " | ").strip(), text, flags=re.MULTILINE)
    text = re.sub(r"^[\|\s\-:]+$", "", text, flags=re.MULTILINE)
    # Remove image references ![alt](url)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Simplify reference-style links [text][ref] to just text
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    # Remove horizontal rules that may confuse block parsing
    text = re.sub(r"^[\s]*[-*_]{3,}[\s]*$", "", text, flags=re.MULTILINE)
    # Collapse excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Slack bot token (direct REST API)
#
# When SLACK_BOT_TOKEN is set, outbound writes prefer the bot so pings come
# from the bot instead of the user's own account (which Slack suppresses from
# its own notifications). The user's MCP OAuth token is still used for reads
# and to invite the bot into channels it is not yet a member of.
# ---------------------------------------------------------------------------


_BOT_USER_ID_CACHE: Optional[str] = None
_BOT_USERNAME_CACHE: Optional[str] = None
_BOT_AUTH_CHECKED: bool = False
_bot_auth_lock = threading.Lock()

_ERROR_DM_THROTTLE_SECS = 60
_error_dm_last_sent: dict = {}
_error_dm_lock = threading.Lock()


def _slack_api(method: str, payload: dict, token: str, timeout: int = 15) -> dict:
    """POST https://slack.com/api/<method> as JSON. Returns parsed body or error dict."""
    req = URLRequest(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"http_{e.code}"}
    except (URLError, TimeoutError) as e:
        return {"ok": False, "error": "network_error", "detail": str(getattr(e, "reason", e))}


def _get_bot_user_id() -> Optional[str]:
    """Run auth.test once, cache the bot's own user ID. None if bot token missing or invalid."""
    global _BOT_USER_ID_CACHE, _BOT_USERNAME_CACHE, _BOT_AUTH_CHECKED
    if not SLACK_BOT_TOKEN:
        return None
    with _bot_auth_lock:
        if _BOT_AUTH_CHECKED:
            return _BOT_USER_ID_CACHE
        _BOT_AUTH_CHECKED = True
        result = _slack_api("auth.test", {}, SLACK_BOT_TOKEN)
        if not result.get("ok"):
            log.warning("SLACK_BOT_TOKEN auth.test failed: %s (bot disabled this session)", result.get("error", "unknown"))
            _BOT_USER_ID_CACHE = None
            return None
        _BOT_USER_ID_CACHE = result.get("user_id") or None
        _BOT_USERNAME_CACHE = result.get("user") or None
        log.info(
            "Bot token OK (user_id=%s, user=%s, team=%s)",
            _BOT_USER_ID_CACHE, _BOT_USERNAME_CACHE, result.get("team", "?"),
        )
        return _BOT_USER_ID_CACHE


def _get_bot_username() -> Optional[str]:
    """Return the bot's handle (e.g. 'mx_adxp_bot'). Requires auth.test to have run."""
    if not _BOT_AUTH_CHECKED:
        _get_bot_user_id()
    return _BOT_USERNAME_CACHE


def _bot_post_message(channel_id: str, thread_ts: str, message: str) -> tuple[bool, str]:
    """Post via bot token. Returns (ok, error_code). error_code is empty when ok."""
    if not SLACK_BOT_TOKEN:
        return (False, "no_bot_token")
    payload: dict = {"channel": channel_id, "text": message}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    result = _slack_api("chat.postMessage", payload, SLACK_BOT_TOKEN)
    if result.get("ok"):
        return (True, "")
    return (False, result.get("error", "unknown"))


def _bot_add_reaction(channel_id: str, message_ts: str, emoji: str) -> tuple[bool, str]:
    if not SLACK_BOT_TOKEN:
        return (False, "no_bot_token")
    result = _slack_api(
        "reactions.add",
        {"channel": channel_id, "timestamp": message_ts, "name": emoji},
        SLACK_BOT_TOKEN,
    )
    if result.get("ok") or result.get("error") == "already_reacted":
        return (True, "")
    return (False, result.get("error", "unknown"))


def _invite_bot_to_channel(user_token: str, channel_id: str, bot_uid: str) -> tuple[bool, str]:
    """Use the user's OAuth token to add the bot to a channel it is not yet in."""
    result = _slack_api(
        "conversations.invite",
        {"channel": channel_id, "users": bot_uid},
        user_token,
    )
    if result.get("ok") or result.get("error") == "already_in_channel":
        return (True, "")
    return (False, result.get("error", "unknown"))


def _notify_bot_error(summary: str, user_id: str = "") -> bool:
    """Best-effort bot DM to ping the user about a send failure.

    Throttled per (target, first 60 chars of summary) so transient flaps do not
    spam. Always returns fast and never raises.
    """
    target = user_id or SLACK_USER_ID
    if not target or not SLACK_BOT_TOKEN:
        return False
    now = time.time()
    key = (target, summary[:60])
    with _error_dm_lock:
        last = _error_dm_last_sent.get(key, 0)
        if now - last < _ERROR_DM_THROTTLE_SECS:
            return False
        _error_dm_last_sent[key] = now
    try:
        ok, err = _bot_post_message(target, "", f":warning: ClaudeRemote bot: {summary}")
        if not ok:
            log.warning("Bot error DM to %s failed: %s", target, err)
        return ok
    except Exception:
        log.exception("Bot error DM crashed")
        return False


def slack_post_message_direct(token: str, channel_id: str, thread_ts: str, message: str) -> bool:
    """Post via user token + chat.postMessage REST. Used as a fallback when MCP blocks the send."""
    result = _slack_api(
        "chat.postMessage",
        {"channel": channel_id, "text": message, **({"thread_ts": thread_ts} if thread_ts else {})},
        token,
    )
    if result.get("ok"):
        return True
    log.error("slack_post_message_direct error: %s", result.get("error"))
    return False


# Errors that mean "bot missing from channel". Retry after inviting.
_BOT_INVITE_RETRY_ERRORS = {"not_in_channel", "channel_not_found"}


def _legacy_user_token_send(token: str, channel_id: str, thread_ts: str, chunk: str) -> bool:
    """Original MCP-first, direct-API-fallback path using the user's OAuth token."""
    args: dict = {"channel_id": channel_id, "message": chunk}
    if thread_ts:
        args["thread_ts"] = thread_ts
    result = _mcp_call("slack_send_message", args, token)
    if result:
        return True
    if isinstance(result, McpError) and result.is_invalid_blocks:
        sanitized = _sanitize_for_slack(chunk)
        log.warning("invalid_blocks (%d chars), retrying sanitized (%d chars)", len(chunk), len(sanitized))
        retry_args = {"channel_id": channel_id, "message": sanitized}
        if thread_ts:
            retry_args["thread_ts"] = thread_ts
        result = _mcp_call("slack_send_message", retry_args, token)
        if result:
            return True
        plaintext = re.sub(r"[*_~`#>\[\]]", "", sanitized)
        plain_args = {"channel_id": channel_id, "message": plaintext}
        if thread_ts:
            plain_args["thread_ts"] = thread_ts
        result = _mcp_call("slack_send_message", plain_args, token)
        if result:
            return True
    log.warning("MCP send failed, falling back to direct user-token API (%d chars)", len(chunk))
    return slack_post_message_direct(token, channel_id, thread_ts, chunk)


def _send_chunk(user_token: str, channel_id: str, thread_ts: str, chunk: str,
                bot_uid: Optional[str], notify_user_id: str) -> bool:
    """Try bot first (auto-inviting on not_in_channel), then user-token fallback. DMs user on every fail path."""
    # 1. Bot post
    if bot_uid:
        ok, err = _bot_post_message(channel_id, thread_ts, chunk)
        if ok:
            return True
        # 2. Auto-invite the bot into the channel and retry once
        if err in _BOT_INVITE_RETRY_ERRORS and channel_id.startswith(("C", "G")):
            log.info("Bot not in %s (%s); attempting invite", channel_id, err)
            invited, invite_err = _invite_bot_to_channel(user_token, channel_id, bot_uid)
            if invited:
                ok, err = _bot_post_message(channel_id, thread_ts, chunk)
                if ok:
                    log.info("Invited bot to %s and posted successfully", channel_id)
                    return True
                log.warning("Bot post to %s still failed after invite: %s", channel_id, err)
                _notify_bot_error(
                    f"Posted invite but bot still cannot post in <#{channel_id}>: {err}. Falling back.",
                    notify_user_id,
                )
            else:
                log.warning("Invite bot to %s failed: %s", channel_id, invite_err)
                _notify_bot_error(
                    f"Bot not in <#{channel_id}> and invite failed ({invite_err}). Falling back.",
                    notify_user_id,
                )
        elif err and err != "no_bot_token":
            # Other bot errors (e.g. invalid_auth, msg_too_long, restricted_action)
            _notify_bot_error(f"Bot post to <#{channel_id}> failed: {err}. Falling back.", notify_user_id)

    # 3. Fall back to user-token MCP/direct path so the bridge keeps working
    if _legacy_user_token_send(user_token, channel_id, thread_ts, chunk):
        return True

    # 4. Everything failed - DM user as bot as a last-chance attention ping
    _notify_bot_error(
        f"Could not post to <#{channel_id}> via bot or user token. Reply was dropped.",
        notify_user_id,
    )
    return False


def mcp_send_message(token: str, channel_id: str, thread_ts: str, message: str,
                     notify_user_id: str = "") -> bool:
    """Send a Slack message. Bot-first, auto-invites bot to channels, falls back to user token.

    `token` is the user's MCP OAuth token (also used for reads). `notify_user_id`
    is the Slack user ID to DM via bot when something fails - defaults to
    SLACK_USER_ID so Al always gets notified.
    """
    chunks = _split_message(message)
    bot_uid = _get_bot_user_id()
    for i, chunk in enumerate(chunks):
        if _send_chunk(token, channel_id, thread_ts, chunk, bot_uid, notify_user_id):
            continue
        log.error("All send methods failed for chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        return False
    return True


def mcp_add_reaction(token: str, channel_id: str, message_ts: str, emoji: str = "eyes") -> bool:
    """Add a reaction. Bot-first with auto-invite, falls back to user token.

    Silent on failure (no DM) since emojis are acknowledgments, not content.
    """
    bot_uid = _get_bot_user_id()
    if bot_uid:
        ok, err = _bot_add_reaction(channel_id, message_ts, emoji)
        if ok:
            return True
        if err in _BOT_INVITE_RETRY_ERRORS and channel_id.startswith(("C", "G")):
            invited, _ = _invite_bot_to_channel(token, channel_id, bot_uid)
            if invited:
                ok, _ = _bot_add_reaction(channel_id, message_ts, emoji)
                if ok:
                    return True

    # Fall back to user token via direct API
    result = _slack_api(
        "reactions.add",
        {"channel": channel_id, "timestamp": message_ts, "name": emoji},
        token,
        timeout=10,
    )
    if result.get("ok") or result.get("error") == "already_reacted":
        return True
    log.warning("reactions.add failed: %s", result.get("error", "unknown"))
    return False


def mcp_resolve_slack_user_id(token: str) -> Optional[str]:
    """Get the current Slack user's ID via MCP read_user_profile (no args = self).

    Returns the user ID string (e.g. 'U03QR8V62PN') or None on failure.
    """
    result = _mcp_call("slack_read_user_profile", {}, token)
    if result is None:
        return None
    raw_text = None
    if isinstance(result, dict) and "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                raw_text = item["text"]
                break
    if not raw_text:
        return None
    # The profile text contains "User ID: U03QR8V62PN" or similar
    match = re.search(r"User ID:\s*(U[A-Z0-9]+)", raw_text)
    if match:
        return match.group(1)
    # Fallback: any U-prefixed ID
    match = re.search(r"\b(U[A-Z0-9]{8,})\b", raw_text)
    return match.group(1) if match else None


def mcp_search_channels(token: str, query: str) -> Optional[str]:
    """Search for Slack channels by name via MCP. Returns channel ID or None."""
    result = _mcp_call("slack_search_channels", {
        "query": query,
        "limit": 5,
        "response_format": "detailed",
    }, token)
    if result is None:
        return None
    raw_text = None
    if isinstance(result, dict) and "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                raw_text = item["text"]
                break
    if raw_text is None:
        return None
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and "results" in parsed:
            raw_text = parsed["results"]
    except (json.JSONDecodeError, ValueError):
        pass
    # Parse channel ID from "Name: #channel-name\n...ID: C123456" or "(ID: C123456)"
    for line in raw_text.splitlines():
        if query.lower() in line.lower():
            match = re.search(r"\((?:ID:\s*)?([A-Z][A-Z0-9]+)\)", line)
            if match:
                return match.group(1)
    # Fallback: find any channel ID in the text
    match = re.search(r"\((?:ID:\s*)?([C][A-Z0-9]+)\)", raw_text)
    return match.group(1) if match else None


def mcp_search_messages(token: str, query: str, limit: int = 20) -> Optional[str]:
    """Search Slack messages across all channels via MCP.

    Returns the search results text, or None on failure.
    The MCP search tool returns {"results": "...", "pagination_info": "..."}.
    """
    result = _mcp_call("slack_search_public_and_private", {
        "query": query,
        "limit": limit,
        "include_context": False,
    }, token)
    if result is None:
        return None
    # Extract text from MCP content wrapper
    raw_text = None
    if isinstance(result, dict) and "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                raw_text = item["text"]
                break
    if raw_text is None:
        return None
    # The text is JSON with a "results" field containing the formatted output
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and "results" in parsed:
            return parsed["results"]
    except (json.JSONDecodeError, ValueError):
        pass
    return raw_text


def parse_search_results(search_text: str) -> list:
    """Parse MCP search result text into structured message dicts.

    The MCP slack_search_public_and_private tool returns formatted text like:

        ### Result 1 of 4
        Channel: #channel-name (ID: C123456)
        From: User Name (ID: U123456)
        Time: 2026-03-15 10:30:00 GMT
        Message_ts: 1710499800.000100
        Reply count: 1
        Permalink: [link](https://team.slack.com/archives/C123/p1710499800000100)
        Text:
        @ClaudeRemote do something

        ---

    Returns list of dicts with channel_id, ts, user_id, text, permalink.
    """
    results = []
    current = {}
    in_text = False

    for line in search_text.splitlines():
        stripped = line.strip()

        # New result block starts
        if stripped.startswith("### Result") or stripped.startswith("=== Message") or stripped.startswith("=== Result"):
            if current.get("ts"):
                results.append(current)
            current = {}
            in_text = False
            continue

        # Separator between results
        if stripped == "---":
            in_text = False
            continue

        # If we're in text-capture mode, accumulate lines
        if in_text:
            if current.get("text"):
                current["text"] += "\n" + stripped
            else:
                current["text"] = stripped
            continue

        # Parse structured fields
        if stripped.startswith("Channel:"):
            # "Channel: #name (ID: C123456)" or "Channel: DM (ID: D123)"
            match = re.search(r"\(ID:\s*([A-Z][A-Z0-9]+)\)", stripped)
            if not match:
                match = re.search(r"\(([A-Z][A-Z0-9]+)\)", stripped)
            if match:
                current["channel_id"] = match.group(1)
        elif stripped.startswith("From:"):
            match = re.search(r"\(ID:\s*([A-Z][A-Z0-9]+)\)", stripped)
            if not match:
                match = re.search(r"\(([A-Z][A-Z0-9]+)\)", stripped)
            if match:
                current["user_id"] = match.group(1)
        elif stripped.startswith("Message_ts:") or stripped.startswith("Message TS:"):
            current["ts"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Thread_ts:") or stripped.startswith("Thread TS:"):
            val = stripped.split(":", 1)[1].strip()
            if val and val not in ("None", "N/A", ""):
                current["thread_ts"] = val
        elif stripped.startswith("Permalink:"):
            # May be "[link](url)" or just a URL
            url_match = re.search(r"\((https?://[^\)]+)\)", stripped)
            if url_match:
                current["permalink"] = url_match.group(1)
            else:
                current["permalink"] = stripped[len("Permalink:"):].strip()
            # MCP search omits a dedicated Thread_ts field and instead encodes
            # the parent as a ?thread_ts=<ts> query parameter on the permalink.
            # Extract it so thread-reply mentions are treated as replies, not
            # as fresh top-level posts.
            if not current.get("thread_ts"):
                tts_match = re.search(r"[?&]thread_ts=([\d.]+)", current.get("permalink", ""))
                if tts_match:
                    current["thread_ts"] = tts_match.group(1)
        elif stripped.startswith("Text:"):
            # Text field - may have content on same line or next lines
            text_val = stripped[len("Text:"):].strip()
            current["text"] = text_val
            in_text = True
        # Skip other fields (Time:, Reply count:, Participants:, etc.)

    # Don't forget the last message
    if current.get("ts"):
        results.append(current)

    return results


# ---------------------------------------------------------------------------
# Slack Message Parsing
# ---------------------------------------------------------------------------


def parse_new_messages(channel_text: str, since_ts: str) -> list:
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


def parse_thread_replies(thread_text: str, since_ts: str) -> list:
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
    """Check if a Slack message should be processed."""
    text = msg.get("text", "")
    # Skip agent output - check multiple markers to prevent self-reply loops
    if AGENT_PREFIX in text:
        return False
    if "Sent using" in text and "Claude" in text:
        return False
    if SLACK_USER_ID and f"<@{SLACK_USER_ID}" in text:
        return False
    if "has joined the channel" in text:
        return False
    if not text.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Slack State Management
# ---------------------------------------------------------------------------


def load_slack_state() -> dict:
    """Load Slack agent state from file."""
    defaults = {
        "channel_id": "",
        "channel_name": SLACK_CHANNEL_NAME,
        "last_checked_ts": "",
        "active_threads": {},
        "search_processed_ids": [],
    }
    if not SLACK_STATE_FILE.exists():
        return defaults
    try:
        state = json.loads(SLACK_STATE_FILE.read_text())
        # Ensure new fields exist in old state files
        for key, val in defaults.items():
            state.setdefault(key, val)
        return state
    except (json.JSONDecodeError, ValueError):
        return defaults


def save_slack_state(state: dict):
    """Save Slack agent state to file."""
    SLACK_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Slack Poll Cycle
# ---------------------------------------------------------------------------


def slack_poll_cycle(token: str, state: dict):
    """Single Slack poll iteration."""
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
    thread_failures = state.get("thread_failures", {})
    for thread_ts, last_reply_ts in list(active_threads.items()):
        thread_text = mcp_read_thread(token, channel_id, thread_ts)
        if thread_text is None:
            count = thread_failures.get(thread_ts, 0) + 1
            thread_failures[thread_ts] = count
            if count >= 3:
                log.info("Removing stale thread %s after %d failures", thread_ts, count)
                session_id = state.get("thread_sessions", {}).pop(thread_ts, None)
                if session_id:
                    delete_claude_session(session_id)
                active_threads.pop(thread_ts, None)
                thread_failures.pop(thread_ts, None)
            else:
                log.info("Thread %s read failed (%d/3)", thread_ts, count)
            continue
        # Reset failure count on success
        thread_failures.pop(thread_ts, None)
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
        # Re-read state under lock to avoid clobbering async thread updates
        with _state_lock:
            state = load_slack_state()
            if newest_ts != last_ts:
                state["last_checked_ts"] = newest_ts
            state["thread_failures"] = thread_failures
            save_slack_state(state)
        return

    log.info(
        "Found %d messages to process (%d top-level, %d thread replies)",
        len(to_process),
        sum(1 for m in to_process if not m["is_thread_reply"]),
        sum(1 for m in to_process if m["is_thread_reply"]),
    )

    # 4. Process each message (non-blocking via thread pool)
    submitted = 0
    for msg in to_process:
        # Rate limit
        allowed, remaining = _check_rate_limit()
        if not allowed:
            log.warning("Rate limit reached, skipping remaining messages")
            mcp_send_message(
                token, channel_id,
                msg.get("thread_ts", msg["ts"]),
                f"{AGENT_PREFIX} Rate limit reached (20/hour). Try again later.",
                notify_user_id=msg.get("user_id", ""),
            )
            break

        text = msg["text"]
        thread_ts = msg.get("thread_ts", msg["ts"]) if msg["is_thread_reply"] else msg["ts"]

        # Skip if already being processed by another thread
        if _is_inflight(msg["ts"]):
            continue

        # Acknowledge receipt with eyes emoji
        mcp_add_reaction(token, channel_id, msg["ts"], "eyes")

        # Transcribe any audio files attached to the message
        audio_preamble = process_slack_audio_files(text)
        if audio_preamble:
            text = audio_preamble + text

        # Determine session: resume existing or start new
        thread_sessions = state.get("thread_sessions", {})
        resume = thread_ts in thread_sessions
        if resume:
            session_id = thread_sessions[thread_ts]
        else:
            session_id = str(uuid.uuid4())
        log.info("Submitting (session=%s, resume=%s): %.80s", session_id[:8], resume, text)

        # Build prompt for Claude
        no_post = (
            f"SLACK POSTING RULE: Your text response will be automatically posted to "
            f"channel {channel_id}, thread {thread_ts}. Do NOT use slack_send_message to "
            f"post in this same channel/thread (it will double-post). However, you MAY use "
            f"slack_send_message to DM other users or post in OTHER channels when the user "
            f"explicitly asks you to. You may use all Slack read/search tools freely."
        )
        if resume:
            prompt = text
        elif msg["is_thread_reply"]:
            thread_context = msg.get("thread_context", "")
            prompt = (
                f"{no_post}\n\n"
                f"You are an AI assistant replying in a Slack thread. "
                f"Here is the full thread context:\n\n{thread_context}\n\n"
                f"The latest message is: {text}\n\n"
                f"Respond to this latest message. Use Slack mrkdwn formatting "
                f"(*bold*, _italic_, `code`). Be concise and helpful. "
                f"Use all available MCP tools if needed."
            )
        else:
            prompt = (
                f"{no_post}\n\n"
                f"You are an AI assistant responding to a Slack message. "
                f"The message is: {text}\n\n"
                f"Process this as a task. Use all available MCP tools "
                f"(Slack, Google Workspace, Glean, etc.) as needed. "
                f"Use Slack mrkdwn formatting (*bold*, _italic_, `code`). "
                f"Be concise and helpful."
            )

        # Build retry prompt for resume failures
        _thread_context = msg.get("thread_context", "")
        _msg_text = text
        def _make_retry(tc=_thread_context, tx=_msg_text, np=no_post):
            if tc:
                return (
                    f"{np}\n\n"
                    f"You are an AI assistant replying in a Slack thread. "
                    f"Here is the full thread context:\n\n{tc}\n\n"
                    f"The latest message is: {tx}\n\n"
                    f"Respond to this latest message. Use Slack mrkdwn formatting "
                    f"(*bold*, _italic_, `code`). Be concise and helpful. "
                    f"Use all available MCP tools if needed."
                )
            return (
                f"{np}\n\n"
                f"You are an AI assistant responding to a Slack message. "
                f"The message is: {tx}\n\n"
                f"Process this as a task. Use all available MCP tools "
                f"(Slack, Google Workspace, Glean, etc.) as needed. "
                f"Use Slack mrkdwn formatting (*bold*, _italic_, `code`). "
                f"Be concise and helpful."
            )

        # State update callback (runs inside _state_lock in the thread)
        _thread_ts = thread_ts
        _msg_ts = msg["ts"]
        def _on_success(st, sid, tts=_thread_ts, mts=_msg_ts):
            st.setdefault("active_threads", {})[tts] = mts
            st.setdefault("thread_sessions", {})[tts] = sid

        _mark_inflight(msg["ts"])
        _executor.submit(
            _async_invoke_and_reply,
            token, channel_id, thread_ts, msg["ts"],
            prompt, session_id, resume, _on_success, _make_retry,
        )
        submitted += 1

    # 5. Update state (thread-safe: re-read state under lock)
    newest_top_ts = last_ts
    for msg in new_top_level:
        if float(msg.get("ts", "0")) > float(newest_top_ts):
            newest_top_ts = msg["ts"]

    # Prune stale threads. Default is aggressive (1 day) since each active
    # thread costs 1 slack_read_thread call per poll cycle, and the Slack MCP
    # rate-limits at ~20/min per method. Tune via CLAUDE_REMOTE_THREAD_TTL_DAYS.
    ttl_days = float(os.environ.get("CLAUDE_REMOTE_THREAD_TTL_DAYS", "1"))
    cutoff = time.time() - ttl_days * 86400
    pruned_sessions = []
    for k, v in list(active_threads.items()):
        if float(v) <= cutoff:
            sid = state.get("thread_sessions", {}).get(k)
            if sid:
                pruned_sessions.append((k, sid))

    with _state_lock:
        state = load_slack_state()
        if newest_top_ts != last_ts:
            state["last_checked_ts"] = newest_top_ts
        state["thread_failures"] = thread_failures
        # Prune stale threads
        for k, sid in pruned_sessions:
            state.get("active_threads", {}).pop(k, None)
            state.get("thread_sessions", {}).pop(k, None)
            delete_claude_session(sid)
        save_slack_state(state)

    log.info("Submitted %d messages to thread pool", submitted)


# ---------------------------------------------------------------------------
# Cross-Channel Slack Cycle
# ---------------------------------------------------------------------------


def _build_cross_channel_triggers() -> tuple[list[str], list[str]]:
    """Return (search_queries, strip_tokens).

    search_queries: one query per trigger form, used with slack_search_public_and_private.
    strip_tokens: every textual form to both match-against and remove from prompts.
    Slack search can return messages with mentions rendered as `<@U...>` OR as
    the plain `@username`, depending on how the MCP formats results - accept both.
    """
    search_queries: list[str] = []
    strip_tokens: list[str] = []
    if CROSS_CHANNEL_TRIGGER:
        search_queries.append(CROSS_CHANNEL_TRIGGER)
        strip_tokens.append(CROSS_CHANNEL_TRIGGER)
    if BOT_MENTION_ENABLED:
        bot_uid = _get_bot_user_id()
        bot_username = _get_bot_username()
        if bot_uid:
            raw = f"<@{bot_uid}>"
            search_queries.append(raw)
            strip_tokens.append(raw)
        if bot_username:
            strip_tokens.append(f"@{bot_username}")
    return search_queries, strip_tokens


def slack_cross_channel_cycle(token: str, state: dict):
    """Search for trigger mentions across all channels and process them.

    Triggers are either the keyword (CROSS_CHANNEL_TRIGGER, default
    `@ClaudeRemote`) or a real @-mention of the bot (<@{bot_uid}>) when a
    bot token is configured. Only explicit mentions are processed - no
    implicit thread follow-ups. Each invocation is submitted to the thread
    pool for concurrent execution.
    """
    if not CROSS_CHANNEL_ENABLED or not SLACK_USER_ID:
        return

    search_queries, strip_tokens = _build_cross_channel_triggers()
    if not search_queries:
        return

    # 1. Search for trigger mentions from today. Run one search per trigger form
    # and dedupe by message ts so the bridge picks up either form.
    today = datetime.now().strftime("%Y-%m-%d")
    all_results = []
    seen_ts: set = set()
    for query_text in search_queries:
        query = f"{query_text} on:{today}"
        search_text = mcp_search_messages(token, query)
        if not search_text:
            continue
        for msg in parse_search_results(search_text):
            ts = msg.get("ts")
            if not ts or ts in seen_ts:
                continue
            seen_ts.add(ts)
            all_results.append(msg)
    if not all_results:
        return

    # 2. Filter out messages we shouldn't process
    private_channel_id = state.get("channel_id", "")
    processed = set(state.get("search_processed_ids", []))

    to_process = []
    for msg in all_results:
        # Security: only process messages from allowed users (or any user in open channels)
        is_open_channel = msg.get("channel_id") in CROSS_CHANNEL_OPEN_CHANNELS
        if not is_open_channel:
            allowed_users = CROSS_CHANNEL_ALLOWED_USERS | {SLACK_USER_ID}
            if msg.get("user_id") not in allowed_users:
                continue
        # Skip private channel (handled by existing poll)
        if msg.get("channel_id") == private_channel_id:
            continue
        # Skip already processed or inflight
        if msg.get("ts") in processed or _is_inflight(msg.get("ts", "")):
            continue
        # Skip agent replies
        if AGENT_PREFIX in msg.get("text", ""):
            continue
        # Must contain at least one trigger form (raw mention, username, or keyword)
        text = msg.get("text", "")
        if not any(t in text for t in strip_tokens):
            # Search returned it but the rendered text has neither form. Trust
            # the search (it would not have matched otherwise) and accept.
            pass
        to_process.append(msg)

    if not to_process:
        return

    # Filter out mentions we already logged as rate-limited so the log doesn't
    # spam "found 1 new mention" every 15s for the same stuck message.
    to_process_fresh = [m for m in to_process if m["ts"] not in _DEFERRED_MENTIONS]
    if to_process_fresh:
        log.info("Cross-channel: found %d new mentions (triggers=%s)",
                 len(to_process_fresh), strip_tokens)

    # 3. Submit each to thread pool
    for msg in to_process:
        if _dispatch_cross_channel_message(token, state, msg, processed, strip_tokens, source="poll"):
            continue
        # Rate-limited - further submissions will also fail this cycle
        break

    # 4. Save processed IDs
    with _state_lock:
        state = load_slack_state()
        existing = set(state.get("search_processed_ids", []))
        existing.update(processed)
        state["search_processed_ids"] = list(existing)[-500:]
        save_slack_state(state)


def _dispatch_cross_channel_message(token: str, state: dict, msg: dict,
                                    processed: set, strip_tokens: list,
                                    source: str = "poll") -> bool:
    """Run rate-limit + context + prompt + submit for a single mention.

    Returns True if accepted (submitted or skipped for a benign reason like
    empty-after-strip), False if deferred by rate limit. Shared by the poll
    cycle and the Socket Mode listener; dedupes via _inflight + processed set.
    """
    ts = msg.get("ts")
    if not ts:
        return True

    if _is_inflight(ts) or ts in processed:
        return True

    allowed, _ = _check_rate_limit()
    if not allowed:
        if ts not in _DEFERRED_MENTIONS:
            _DEFERRED_MENTIONS.add(ts)
            log.warning(
                "Rate limit reached (%d/hr). Deferred %s in %s until budget frees.",
                RATE_LIMIT_PER_HOUR, ts, msg.get("channel_id", "?"),
            )
        return False
    _DEFERRED_MENTIONS.discard(ts)

    # Strip every known trigger form (keyword, raw bot mention, @username)
    text = msg.get("text", "")
    for tok in strip_tokens:
        text = text.replace(tok, "")
    text = text.strip()
    if not text:
        processed.add(ts)
        return True

    channel_id = msg["channel_id"]
    is_thread_reply = bool(msg.get("thread_ts"))
    thread_ts = msg.get("thread_ts") or ts
    is_dm = channel_id.startswith("D")

    # Acknowledge receipt
    mcp_add_reaction(token, channel_id, ts, "eyes")

    # Fetch the full thread (parent + all replies, including prior bot
    # responses) so follow-ups like "yes, go with A" have context.
    thread_context = mcp_read_thread(token, channel_id, thread_ts) or ""

    # Expand up to 3 Slack permalinks in the thread so references to other
    # threads/messages come along. Skip the thread we just fetched.
    link_context = _expand_slack_permalinks(
        token,
        thread_context + "\n" + text,
        skip={(channel_id, thread_ts)},
    )

    # Resume an existing Claude session for this (channel, thread_ts) so
    # replies in the same thread / DM carry implicit memory.
    session_key = f"{channel_id}:{thread_ts}"
    thread_sessions = state.get("thread_sessions", {})
    resume = session_key in thread_sessions
    session_id = thread_sessions[session_key] if resume else str(uuid.uuid4())

    log.info(
        "Dispatch %s (channel=%s, session=%s, resume=%s, reply=%s, dm=%s): %.80s",
        source, channel_id, session_id[:8], resume, is_thread_reply, is_dm, text,
    )

    no_post_rule = (
        f"SLACK POSTING RULE: Your text response will be automatically posted to "
        f"channel {channel_id}, thread {thread_ts}. Do NOT use slack_send_message to "
        f"post in this same channel/thread (it will double-post). However, you MAY use "
        f"slack_send_message to DM other users or post in OTHER channels when the user "
        f"explicitly asks you to. You may use all Slack read/search tools freely."
    )
    context_instruction = (
        "Before answering, read the FULL thread below - including prior replies from "
        ":robot_face: (that is you in an earlier turn). If the latest message refers to "
        "earlier content (e.g. 'option A', 'that link'), ground your answer in what was "
        "actually said above. If any URLs (Confluence/Sigma/Chronosphere/Jira/etc.) or "
        "Slack permalinks in the thread are relevant and not already expanded below, "
        "use WebFetch, mcp__plugin_jira, or the appropriate MCP tool to read them before "
        "responding."
    )

    def _build_prompt(t: str, tc: str, lc: str) -> str:
        if is_dm:
            role = "You are responding to a DM sent directly to you."
        elif is_thread_reply:
            role = "You are replying in an existing Slack thread."
        else:
            role = "You are responding to a Slack message that tagged you."
        parts: list[str] = [no_post_rule, "", role, "", context_instruction, ""]
        parts.append(f"Channel: <#{channel_id}>")
        parts.append(f"Thread ts: {thread_ts}")
        if tc:
            parts.append("")
            parts.append("=== Full thread context ===")
            parts.append(tc)
            parts.append("=== End of thread ===")
        if lc:
            parts.append("")
            parts.append("=== Linked Slack threads (already expanded) ===")
            parts.append(lc)
            parts.append("=== End of linked threads ===")
        parts.append("")
        parts.append(f"The user's latest message is: {t}")
        parts.append("")
        parts.append(
            "Respond with Slack mrkdwn formatting (*bold*, _italic_, `code`). "
            "Be concise and helpful. Do NOT repeat prior analysis verbatim - "
            "build on it."
        )
        return "\n".join(parts)

    prompt = _build_prompt(text, thread_context, link_context)

    def _on_success(st, sid, mts=ts, sk=session_key):
        ids = set(st.get("search_processed_ids", []))
        ids.add(mts)
        st["search_processed_ids"] = list(ids)[-500:]
        st.setdefault("thread_sessions", {})[sk] = sid

    def _make_retry(t=text, tc=thread_context, lc=link_context, build=_build_prompt):
        return build(t, tc, lc)

    processed.add(ts)
    _mark_inflight(ts)
    _executor.submit(
        _async_invoke_and_reply,
        token, channel_id, thread_ts, ts,
        prompt, session_id, resume, _on_success, _make_retry,
        notify_user_id=msg.get("user_id", ""),
    )
    return True


# ---------------------------------------------------------------------------
# Unified Run Bridge
# ---------------------------------------------------------------------------


def run_bridge(foreground: bool = False, gmail_enabled: bool = True, slack_enabled: bool = False):
    """Main loop: poll Gmail and/or Slack, process messages, reply."""
    global _startup_time, _messages_processed, _executor
    setup_logging(foreground=foreground)
    _messages_processed = 0
    _startup_time = datetime.now(timezone.utc)
    _executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_INVOCATIONS, thread_name_prefix="claude")

    transports = []
    if gmail_enabled:
        transports.append("Gmail")
    if slack_enabled:
        transports.append("Slack")
    log.info("Starting ClaudeRemote (%s)", " + ".join(transports))
    if slack_enabled and BUSINESS_HOURS_ONLY:
        log.info("Slack business hours: %d:00 - %d:00", BUSINESS_HOURS_START, BUSINESS_HOURS_END)

    # --- Gmail init ---
    service = None
    my_email = None
    processed_ids = None
    thread_sessions = None
    startup_time_ms = None

    if gmail_enabled:
        try:
            service = authenticate()
            my_email = get_my_email(service)
            log.info("Gmail authenticated as %s", my_email)
            processed_ids = load_processed_ids()
            thread_sessions = load_thread_sessions()
            startup_time_ms = int(time.time() * 1000)
            log.info("Loaded %d processed IDs, %d thread sessions", len(processed_ids), len(thread_sessions))
        except Exception as e:
            log.error("Gmail authentication failed: %s", e)
            if not slack_enabled:
                print(f"Gmail authentication failed: {e}", file=sys.stderr)
                sys.exit(1)
            else:
                log.warning("Continuing with Slack only (Gmail init failed)")
                gmail_enabled = False

    # --- Slack init ---
    slack_token = None
    slack_state = None

    if slack_enabled:
        try:
            slack_token = get_slack_token()
            if not slack_token:
                raise RuntimeError(
                    "No valid Slack MCP token found. "
                    "Run any Slack MCP tool in Claude Code first to authenticate."
                )
            log.info("Slack MCP token loaded successfully")
            # Eager-validate the bot token so auth.test failures surface at startup,
            # not on the first outbound message.
            if SLACK_BOT_TOKEN:
                bot_uid = _get_bot_user_id()
                if bot_uid:
                    log.info("Slack bot enabled (user_id=%s) - posting as bot", bot_uid)
                else:
                    log.warning("SLACK_BOT_TOKEN set but invalid - falling back to user-token posting")
            else:
                log.info("SLACK_BOT_TOKEN not set - posting as user (no self-notifications)")
            log.info(
                "Slack mode: private_poll=%s, cross_channel=%s, keyword_trigger=%r, bot_mention=%s, rate_limit=%d/hr",
                not DISABLE_PRIVATE_POLL, CROSS_CHANNEL_ENABLED,
                CROSS_CHANNEL_TRIGGER or "(disabled)", BOT_MENTION_ENABLED,
                RATE_LIMIT_PER_HOUR,
            )
            slack_state = load_slack_state()
            if not slack_state["channel_id"]:
                log.info("No channel_id in state - resolving #%s via MCP search", SLACK_CHANNEL_NAME)
                channel_id = mcp_search_channels(slack_token, SLACK_CHANNEL_NAME)
                if not channel_id:
                    raise RuntimeError(
                        f"Could not find channel #{SLACK_CHANNEL_NAME} in Slack. "
                        f"Create the channel first, then restart the bridge."
                    )
                slack_state["channel_id"] = channel_id
                slack_state["channel_name"] = SLACK_CHANNEL_NAME
                save_slack_state(slack_state)
                log.info("Resolved #%s -> %s", SLACK_CHANNEL_NAME, channel_id)
            # Auto-resolve SLACK_USER_ID if not set via env
            global SLACK_USER_ID
            if not SLACK_USER_ID:
                log.info("SLACK_USER_ID not set, resolving via MCP...")
                resolved_uid = mcp_resolve_slack_user_id(slack_token)
                if resolved_uid:
                    SLACK_USER_ID = resolved_uid
                    log.info("Auto-resolved SLACK_USER_ID -> %s", SLACK_USER_ID)
                else:
                    log.warning(
                        "Could not auto-resolve SLACK_USER_ID. "
                        "Cross-channel @ClaudeRemote mentions will be disabled. "
                        "Set CLAUDE_REMOTE_SLACK_USER_ID env var to fix."
                    )
            if not slack_state["last_checked_ts"]:
                slack_state["last_checked_ts"] = f"{time.time():.6f}"
                save_slack_state(slack_state)
                log.info("Slack first run - initialized timestamp")
            log.info(
                "Watching #%s (%s), %d active threads",
                slack_state["channel_name"],
                slack_state["channel_id"],
                len(slack_state.get("active_threads", {})),
            )
        except Exception as e:
            log.error("Slack init failed: %s", e)
            if not gmail_enabled:
                print(f"Slack init failed: {e}", file=sys.stderr)
                sys.exit(1)
            else:
                log.warning("Continuing with Gmail only (Slack init failed)")
                slack_enabled = False

    if not gmail_enabled and not slack_enabled:
        print("Error: No transport could be initialized.", file=sys.stderr)
        sys.exit(1)

    log.info("Startup time: %s", _startup_time.isoformat())

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        # --- Slack poll ---
        if slack_enabled:
            if not is_business_hours():
                log.debug("Outside business hours, skipping Slack poll")
            else:
                # Re-read token each cycle in case Claude Code refreshed it
                slack_token = get_slack_token()
                if not slack_token:
                    log.warning("Slack token expired or missing")
                else:
                    # Private-channel poll (disabled when DISABLE_PRIVATE_POLL=true)
                    if not DISABLE_PRIVATE_POLL:
                        try:
                            log.info("Starting Slack poll cycle")
                            slack_state = load_slack_state()
                            slack_poll_cycle(slack_token, slack_state)
                            log.info("Slack poll cycle complete")
                        except Exception:
                            log.exception("Error in Slack poll cycle")

                    # Cross-channel @MXADXP / @ClaudeRemote mention search
                    if CROSS_CHANNEL_ENABLED:
                        try:
                            slack_state = load_slack_state()
                            slack_cross_channel_cycle(slack_token, slack_state)
                        except Exception:
                            log.exception("Error in cross-channel Slack cycle")

        # --- Gmail poll ---
        if gmail_enabled:
            try:
                log.info("Starting Gmail poll cycle")
                gmail_poll_cycle(service, my_email, processed_ids, thread_sessions, startup_time_ms)
                log.info("Gmail poll cycle complete")
            except Exception:
                log.exception("Error in Gmail poll cycle")
                # Re-authenticate in case token issues
                try:
                    service = authenticate()
                except Exception:
                    log.exception("Gmail re-authentication failed")

        # Sleep in small increments so we respond to signals
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("Waiting for in-flight invocations to complete...")
    _executor.shutdown(wait=True)
    log.info("ClaudeRemote stopped")


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
        if "bridge.py" not in line or "slack_bridge" in line or "slack_mcp_bridge" in line:
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


def start_daemon(gmail_enabled: bool = True, slack_enabled: bool = False):
    """Start the bridge as a background subprocess."""
    # Check if already running
    pids = _find_bridge_pids()
    if pids:
        print(f"Bridge already running (PIDs {pids})")
        return

    # Build the run command with transport flags
    cmd = [sys.executable, __file__, "run"]
    if gmail_enabled and slack_enabled:
        cmd.append("--all")
    elif slack_enabled:
        cmd.append("--slack")
    else:
        cmd.append("--gmail")

    # Launch as a detached subprocess
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))

    transports = []
    if gmail_enabled:
        transports.append("Gmail")
    if slack_enabled:
        transports.append("Slack")
    print(f"Bridge started (PID {proc.pid}) [{' + '.join(transports)}]")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    if slack_enabled and BUSINESS_HOURS_ONLY:
        print(f"  Slack business hours: {BUSINESS_HOURS_START}:00 - {BUSINESS_HOURS_END}:00")
    print(f"  Logs: {LOG_FILE}")


def stop_daemon():
    """Stop all running bridge daemon(s)."""
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


def show_status():
    """Show bridge status including both transports."""
    pids = _find_bridge_pids()
    if pids:
        print(f"Bridge is running (PIDs {pids})")
    else:
        if PID_FILE.exists():
            file_pid = PID_FILE.read_text().strip()
            if _pid_alive(int(file_pid)):
                print(f"Bridge is running (PID {file_pid})")
            else:
                print("Bridge is not running (stale PID file)")
        else:
            print("Bridge is not running")

    # Gmail status
    print("\n[Gmail]")
    if TOKEN_FILE.exists():
        print("  Token: present")
    else:
        print("  Token: not configured")
    if GMAIL_PROCESSED_FILE.exists():
        count = len(GMAIL_PROCESSED_FILE.read_text().strip().splitlines())
        print(f"  Processed messages: {count}")
    if SESSIONS_FILE.exists():
        try:
            sessions = json.loads(SESSIONS_FILE.read_text())
            print(f"  Active threads: {len(sessions)}")
        except (json.JSONDecodeError, ValueError):
            pass

    # Slack status
    print("\n[Slack MCP]")
    slack_state = load_slack_state()
    print(f"  Channel: #{slack_state.get('channel_name', '?')} ({slack_state.get('channel_id', '?')})")
    print(f"  Last checked: {slack_state.get('last_checked_ts', 'never')}")
    print(f"  Active threads: {len(slack_state.get('active_threads', {}))}")

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


def _parse_transport_flags(args) -> tuple:
    """Parse --gmail, --slack, --all flags. Returns (gmail_enabled, slack_enabled)."""
    if args.all:
        return True, True
    if args.slack and not args.gmail:
        return False, True
    if args.gmail and not args.slack:
        return True, False
    # Default: --gmail only (backward compatible)
    if not args.gmail and not args.slack:
        return True, False
    return args.gmail, args.slack


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="ClaudeRemote: unified Gmail + Slack remote interface for Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s run --gmail       # Gmail only (default)\n"
            "  %(prog)s run --slack       # Slack MCP only\n"
            "  %(prog)s run --all         # Both Gmail + Slack\n"
            "  %(prog)s start --all       # Daemon with both transports\n"
            "  %(prog)s stop              # Stop daemon\n"
            "  %(prog)s status            # Show status\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run subcommand
    run_parser = subparsers.add_parser("run", help="Run in foreground")
    run_parser.add_argument("--gmail", action="store_true", help="Enable Gmail transport")
    run_parser.add_argument("--slack", action="store_true", help="Enable Slack MCP transport")
    run_parser.add_argument("--all", action="store_true", help="Enable both Gmail + Slack")

    # start subcommand
    start_parser = subparsers.add_parser("start", help="Start as background daemon")
    start_parser.add_argument("--gmail", action="store_true", help="Enable Gmail transport")
    start_parser.add_argument("--slack", action="store_true", help="Enable Slack MCP transport")
    start_parser.add_argument("--all", action="store_true", help="Enable both Gmail + Slack")

    # stop subcommand
    subparsers.add_parser("stop", help="Stop daemon")

    # status subcommand
    subparsers.add_parser("status", help="Show bridge status")

    # test-bot subcommand: validate SLACK_BOT_TOKEN end-to-end
    test_bot = subparsers.add_parser("test-bot", help="Validate SLACK_BOT_TOKEN and send a test DM")
    test_bot.add_argument("--to", default="", help="Slack user ID to DM (default: SLACK_USER_ID)")
    test_bot.add_argument("--channel", default="", help="Channel ID to post a test message in (optional)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        gmail_enabled, slack_enabled = _parse_transport_flags(args)
        run_bridge(foreground=True, gmail_enabled=gmail_enabled, slack_enabled=slack_enabled)
    elif args.command == "start":
        gmail_enabled, slack_enabled = _parse_transport_flags(args)
        start_daemon(gmail_enabled=gmail_enabled, slack_enabled=slack_enabled)
    elif args.command == "stop":
        stop_daemon()
    elif args.command == "status":
        show_status()
    elif args.command == "test-bot":
        sys.exit(_cmd_test_bot(args))
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_test_bot(args) -> int:
    """Validate bot token: run auth.test, DM the user, optionally post in a channel."""
    setup_logging(foreground=True)
    if not SLACK_BOT_TOKEN:
        print("ERROR: SLACK_BOT_TOKEN is not set. Add it to .env and retry.", file=sys.stderr)
        return 1
    print(f"Testing bot token (prefix: {SLACK_BOT_TOKEN[:10]}...)")
    bot_uid = _get_bot_user_id()
    if not bot_uid:
        print("ERROR: auth.test failed. Token is invalid or the bot lacks scopes.", file=sys.stderr)
        return 1
    print(f"OK: auth.test passed. Bot user_id = {bot_uid}")

    target = args.to or SLACK_USER_ID or os.environ.get("CLAUDE_REMOTE_SLACK_USER_ID", "")
    if not target:
        print("WARN: no --to and no SLACK_USER_ID; skipping DM test")
    else:
        ok, err = _bot_post_message(
            target, "",
            ":wave: ClaudeRemote bot test DM. If you see this, error pings will work."
        )
        if ok:
            print(f"OK: DM sent to {target}")
        else:
            print(f"ERROR: DM to {target} failed: {err}", file=sys.stderr)
            return 1

    if args.channel:
        user_token = get_slack_token()
        if not user_token:
            print("WARN: no user MCP token available; cannot auto-invite on not_in_channel", file=sys.stderr)
        ok = _send_chunk(
            user_token or "", args.channel, "",
            ":robot_face: ClaudeRemote bot test message.",
            bot_uid, target,
        )
        print(f"{'OK' if ok else 'ERROR'}: channel test post to {args.channel}")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    main()
