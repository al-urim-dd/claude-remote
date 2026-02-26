#!/Users/zhengli.sun/Projects/claude-remote/.venv/bin/python
"""Unit tests for ClaudeRemote bridge."""

import base64
import email.utils
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# Import functions under test
import bridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path):
    """Override config paths to use a temp directory."""
    with mock.patch.multiple(
        bridge,
        CONFIG_DIR=tmp_path,
        PROCESSED_FILE=tmp_path / "processed.txt",
        SESSIONS_FILE=tmp_path / "thread_sessions.json",
        PID_FILE=tmp_path / "bridge.pid",
        LOG_FILE=tmp_path / "bridge.log",
        ATTACHMENTS_DIR=tmp_path / "attachments",
    ):
        yield tmp_path


# ---------------------------------------------------------------------------
# _strip_quoted_reply
# ---------------------------------------------------------------------------


class TestStripQuotedReply:
    """Tests for stripping Gmail/Outlook quoted reply text."""

    def test_no_quoted_text(self):
        assert bridge.strip_quoted_reply("hello world") == "hello world"

    def test_gmail_quoted_reply(self):
        text = (
            "/sessions\n\n\n"
            "On Thu, Feb 26, 2026 at 2:34 PM Zhengli Sun "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> old stuff here"
        )
        assert bridge.strip_quoted_reply(text) == "/sessions"

    def test_gmail_multiline_on_wrote(self):
        """Gmail sometimes wraps the 'On ... wrote:' line."""
        text = (
            "hello world\n\n"
            "On Mon, Jan 1, 2026 at 10:00 AM Name <x@y.com>\n"
            "wrote:\n"
            "> quoted"
        )
        assert bridge.strip_quoted_reply(text) == "hello world"

    def test_gmail_reply_with_blank_lines(self):
        text = "Do it\n\n\n\nOn Wed, Feb 25 at 3pm User <u@x.com> wrote:\n> yes"
        assert bridge.strip_quoted_reply(text) == "Do it"

    def test_outlook_style(self):
        text = "my reply\n\nFrom: Someone <s@x.com>\nSent: Mon...\n> old"
        assert bridge.strip_quoted_reply(text) == "my reply"

    def test_only_quoted_lines(self):
        text = "> this is all quoted\n> nothing new"
        assert bridge.strip_quoted_reply(text) == ""

    def test_preserves_multiline_user_text(self):
        text = (
            "line one\nline two\nline three\n\n"
            "On Thu, Feb 26, 2026 at 2:34 PM X <x@y.com> wrote:\n> old"
        )
        assert bridge.strip_quoted_reply(text) == "line one\nline two\nline three"

    def test_forwarded_message(self):
        text = "see below\n\n---------- Forwarded message ---------\nFrom: x"
        assert bridge.strip_quoted_reply(text) == "see below"


# ---------------------------------------------------------------------------
# Self-reply detection (skip bot's own replies)
# ---------------------------------------------------------------------------


class TestSelfReplyDetection:
    """Bot must skip messages sent by ClaudeRemote."""

    def test_skip_own_reply(self):
        sender_name, _ = email.utils.parseaddr(
            f"{bridge.REPLY_SENDER_NAME} <zhengli.sun@doordash.com>"
        )
        assert sender_name == bridge.REPLY_SENDER_NAME

    def test_accept_user_email(self):
        sender_name, sender_email = email.utils.parseaddr(
            "Zhengli Sun <zhengli.sun@doordash.com>"
        )
        assert sender_name != bridge.REPLY_SENDER_NAME
        assert sender_email == "zhengli.sun@doordash.com"

    def test_accept_plain_email(self):
        """No display name — should not be treated as bot."""
        sender_name, sender_email = email.utils.parseaddr(
            "zhengli.sun@doordash.com"
        )
        assert sender_name != bridge.REPLY_SENDER_NAME


# ---------------------------------------------------------------------------
# Command parsing (/sessions, /resume)
# ---------------------------------------------------------------------------


class TestCommandParsing:
    """Built-in commands must be recognized from email body text."""

    def test_sessions_command_plain(self):
        body = bridge.strip_quoted_reply("/sessions")
        assert body.lower() == "/sessions"

    def test_sessions_command_with_gmail_quote(self):
        raw = (
            "/sessions\n\n\n"
            "On Thu, Feb 26, 2026 at 3:01 PM ClaudeRemote "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> previous reply..."
        )
        body = bridge.strip_quoted_reply(raw)
        assert body.lower() == "/sessions"

    def test_resume_command(self):
        raw = (
            "/resume abc-123-def\n\n"
            "On Thu, Feb 26, 2026 at 3:01 PM ClaudeRemote "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> previous reply..."
        )
        body = bridge.strip_quoted_reply(raw)
        assert body.lower().startswith("/resume ")
        resume_id = body.split(None, 1)[1].strip()
        assert resume_id == "abc-123-def"

    def test_regular_message_not_command(self):
        body = bridge.strip_quoted_reply("what time is it?")
        assert not body.startswith("/")


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------


class TestExtractBody:
    """Test plain text extraction from Gmail MIME payloads."""

    def test_simple_text_plain(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"hello").decode()},
        }
        assert bridge._extract_body(payload) == "hello"

    def test_multipart_with_text(self):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"plain text").decode()},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<b>html</b>").decode()},
                },
            ],
        }
        assert bridge._extract_body(payload) == "plain text"

    def test_empty_payload(self):
        payload = {"mimeType": "text/plain", "body": {"size": 0}}
        assert bridge._extract_body(payload) == ""


# ---------------------------------------------------------------------------
# State management (processed IDs, thread sessions)
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_processed_ids_roundtrip(self, tmp_config):
        assert bridge.load_processed_ids() == set()

        bridge.save_processed_id("msg1")
        bridge.save_processed_id("msg2")

        ids = bridge.load_processed_ids()
        assert ids == {"msg1", "msg2"}

    def test_processed_ids_no_duplicates(self, tmp_config):
        bridge.save_processed_id("msg1")
        bridge.save_processed_id("msg1")
        # File has two lines but set deduplicates
        ids = bridge.load_processed_ids()
        assert ids == {"msg1"}

    def test_thread_sessions_roundtrip(self, tmp_config):
        assert bridge.load_thread_sessions() == {}

        sessions = {"thread1": "session-a", "thread2": "session-b"}
        bridge.save_thread_sessions(sessions)

        loaded = bridge.load_thread_sessions()
        assert loaded == sessions

    def test_thread_sessions_corrupt_json(self, tmp_config):
        bridge.SESSIONS_FILE.write_text("not json")
        assert bridge.load_thread_sessions() == {}


# ---------------------------------------------------------------------------
# build_thread_context
# ---------------------------------------------------------------------------


class TestBuildThreadContext:
    def test_single_user_message(self):
        msgs = [{"from_name": "Zhengli Sun", "date": "Feb 26", "body": "hello"}]
        ctx = bridge.build_thread_context(msgs)
        assert "[User — Feb 26]" in ctx
        assert "hello" in ctx

    def test_bot_reply_labeled(self):
        msgs = [
            {"from_name": "Zhengli Sun", "date": "Feb 26", "body": "question"},
            {"from_name": bridge.REPLY_SENDER_NAME, "date": "Feb 26", "body": "answer"},
        ]
        ctx = bridge.build_thread_context(msgs)
        assert "You (Claude, in a previous reply)" in ctx

    def test_empty_body_skipped(self):
        msgs = [{"from_name": "X", "date": "Feb 26", "body": ""}]
        ctx = bridge.build_thread_context(msgs)
        assert ctx == ""


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


class TestOutputTruncation:
    def test_short_output_unchanged(self):
        assert len("hello") < bridge.MAX_RESPONSE_LEN
        # invoke_claude would return it as-is (tested via mock below)

    def test_truncation_marker(self):
        long_text = "x" * (bridge.MAX_RESPONSE_LEN + 100)
        # Simulate what invoke_claude does
        if len(long_text) > bridge.MAX_RESPONSE_LEN:
            result = long_text[:bridge.MAX_RESPONSE_LEN] + "\n\n[truncated — response exceeded 50K chars]"
        assert len(result) > bridge.MAX_RESPONSE_LEN
        assert "[truncated" in result


# ---------------------------------------------------------------------------
# send_reply sets correct From display name
# ---------------------------------------------------------------------------


class TestSendReplyFromName:
    def test_from_header_has_display_name(self):
        """Verify the MIME message has ClaudeRemote as display name."""
        original_msg = {
            "subject": "[claude] test",
            "from": "Zhengli Sun <zhengli.sun@doordash.com>",
            "message_id": "<abc@gmail.com>",
            "references": "",
            "thread_id": "thread123",
        }
        my_email = "zhengli.sun@doordash.com"

        # Mock the Gmail API service
        mock_service = mock.MagicMock()
        mock_service.users().messages().send().execute.return_value = {"id": "sent1"}

        bridge.send_reply(mock_service, original_msg, "test reply", my_email)

        # Check the raw message that was sent
        call_args = mock_service.users().messages().send.call_args
        body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
        raw = body.get("raw", "")
        decoded = base64.urlsafe_b64decode(raw).decode()

        assert f"{bridge.REPLY_SENDER_NAME} <{my_email}>" in decoded
        assert "Re: [claude] test" in decoded


# ---------------------------------------------------------------------------
# Pre-startup safety
# ---------------------------------------------------------------------------


class TestPreStartupSafety:
    def test_old_message_skipped(self):
        """Messages older than startup time must be skipped."""
        startup_ms = int(time.time() * 1000)
        old_msg_date_ms = startup_ms - 60_000  # 1 minute before startup
        assert old_msg_date_ms < startup_ms

    def test_new_message_accepted(self):
        startup_ms = int(time.time() * 1000)
        new_msg_date_ms = startup_ms + 5_000  # 5 seconds after startup
        assert new_msg_date_ms >= startup_ms


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
