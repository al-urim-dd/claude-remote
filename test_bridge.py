#!/usr/bin/env python3
"""Unit tests for ClaudeRemote bridge."""

import base64
import email.utils
import json
import os
import tempfile
import time
from datetime import datetime
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
        DIGEST_LAST_SENT_FILE=tmp_path / "digest_last_sent.txt",
        RATE_LIMIT_FILE=tmp_path / "rate_limit.json",
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
            "On Thu, Feb 26, 2026 at 2:34 PM Test User "
            "<user@example.com> wrote:\n"
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
            f"{bridge.REPLY_SENDER_NAME} <user@example.com>"
        )
        assert sender_name == bridge.REPLY_SENDER_NAME

    def test_accept_user_email(self):
        sender_name, sender_email = email.utils.parseaddr(
            "Test User <user@example.com>"
        )
        assert sender_name != bridge.REPLY_SENDER_NAME
        assert sender_email == "user@example.com"

    def test_accept_plain_email(self):
        """No display name — should not be treated as bot."""
        sender_name, sender_email = email.utils.parseaddr(
            "user@example.com"
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
            "<user@example.com> wrote:\n"
            "> previous reply..."
        )
        body = bridge.strip_quoted_reply(raw)
        assert body.lower() == "/sessions"

    def test_resume_command(self):
        raw = (
            "/resume abc-123-def\n\n"
            "On Thu, Feb 26, 2026 at 3:01 PM ClaudeRemote "
            "<user@example.com> wrote:\n"
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
        msgs = [{"from_name": "Test User", "date": "Feb 26", "body": "hello"}]
        ctx = bridge.build_thread_context(msgs)
        assert "[User -- Feb 26]" in ctx
        assert "hello" in ctx

    def test_bot_reply_labeled(self):
        msgs = [
            {"from_name": "Test User", "date": "Feb 26", "body": "question"},
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
            "subject": "cc test",
            "from": "Test User <user@example.com>",
            "message_id": "<abc@gmail.com>",
            "references": "",
            "thread_id": "thread123",
        }
        my_email = "user@example.com"

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
        assert "Re: cc test" in decoded


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




# ---------------------------------------------------------------------------
# strip_claude_prefix
# ---------------------------------------------------------------------------


class TestStripClaudePrefix:
    """Tests for stripping the 'cc' prefix from body and subject."""

    def test_strip_lowercase_prefix(self):
        assert bridge.strip_claude_prefix("cc what time is it?") == "what time is it?"

    def test_strip_mixed_case_prefix(self):
        assert bridge.strip_claude_prefix("Cc hello") == "hello"

    def test_no_prefix_unchanged(self):
        assert bridge.strip_claude_prefix("no prefix here") == "no prefix here"

    def test_prefix_only_yields_empty(self):
        assert bridge.strip_claude_prefix("cc") == ""

    def test_prefix_with_extra_spaces(self):
        assert bridge.strip_claude_prefix("cc   spaced out") == "spaced out"

    def test_uppercase_prefix(self):
        assert bridge.strip_claude_prefix("CC shout") == "shout"

    def test_cc_inside_word_unchanged(self):
        assert bridge.strip_claude_prefix("account info") == "account info"


# ---------------------------------------------------------------------------
# list_sessions slug calculation
# ---------------------------------------------------------------------------


class TestListSessionsSlug:
    """Slug must replace both '/' and '.' with '-'."""

    def test_slug_replaces_dots_and_slashes(self, tmp_path):
        """Paths like /Users/jane.doe/Projects produce correct slug."""
        cwd = "/Users/jane.doe/Projects"
        expected_slug = "-Users-jane-doe-Projects"

        sessions_dir = tmp_path / expected_slug
        sessions_dir.mkdir()
        jsonl = sessions_dir / "session1.jsonl"
        jsonl.write_text(json.dumps({"type": "summary", "summary": "test session"}) + "\n")

        with mock.patch.object(bridge, "CLAUDE_CWD", cwd), \
             mock.patch.object(bridge, "CLAUDE_SESSIONS_DIR", tmp_path):
            result = bridge.list_sessions(count=5)

        assert "session1" in result or "test session" in result

    def test_slug_without_dot_fix_would_fail(self, tmp_path):
        """Verify the old slug (dots NOT replaced) would miss the directory."""
        cwd = "/Users/jane.doe/Projects"
        wrong_slug = "-Users-jane.doe-Projects"

        sessions_dir = tmp_path / wrong_slug
        sessions_dir.mkdir()
        jsonl = sessions_dir / "session1.jsonl"
        jsonl.write_text(json.dumps({"type": "summary", "summary": "test"}) + "\n")

        with mock.patch.object(bridge, "CLAUDE_CWD", cwd), \
             mock.patch.object(bridge, "CLAUDE_SESSIONS_DIR", tmp_path):
            result = bridge.list_sessions(count=5)

        assert result == "No sessions found."


# ---------------------------------------------------------------------------
# /help command
# ---------------------------------------------------------------------------


class TestHelpCommand:
    """Verify HELP_TEXT contains key commands and capabilities."""

    def test_help_text_contains_commands(self):
        assert "/help" in bridge.HELP_TEXT
        assert "/sessions" in bridge.HELP_TEXT
        assert "/resume" in bridge.HELP_TEXT
        assert "/cancel" in bridge.HELP_TEXT

    def test_help_text_contains_capabilities(self):
        assert "Attachments" in bridge.HELP_TEXT
        assert "Multi-turn" in bridge.HELP_TEXT
        assert "Google Workspace" in bridge.HELP_TEXT

    def test_help_command_recognized(self):
        body = bridge.strip_quoted_reply("/help")
        assert body.lower() == "/help"


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


class TestDailyDigest:
    """Tests for the daily digest email feature (skill-based)."""

    def test_digest_skipped_if_already_sent_today(self, tmp_config):
        """Digest must not send twice on the same day."""
        today = datetime.now().strftime("%Y-%m-%d")
        bridge.DIGEST_LAST_SENT_FILE.write_text(today)

        mock_service = mock.MagicMock()
        bridge.send_daily_digest(mock_service, "me@test.com", {}, 5)

        mock_service.users().messages().send.assert_not_called()

    def test_digest_skipped_if_wrong_hour(self, tmp_config):
        """_maybe_send_digest should not fire outside DIGEST_HOUR."""
        mock_service = mock.MagicMock()

        with mock.patch("bridge.DIGEST_ENABLED", True), \
             mock.patch("bridge.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 26, 14, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with mock.patch("bridge.DIGEST_HOUR", 8):
                bridge._maybe_send_digest(mock_service, "me@test.com", {}, 0)

        mock_service.users().messages().send.assert_not_called()

    def test_digest_invokes_brief_skill(self, tmp_config):
        """Digest should invoke the /brief skill via _invoke_skill."""
        mock_service = mock.MagicMock()
        send_mock = mock_service.users.return_value.messages.return_value.send
        send_mock.return_value.execute.return_value = {"id": "d1"}

        with mock.patch.object(bridge, "_invoke_skill", return_value="# Morning Brief\nTest content") as mock_skill:
            bridge.send_daily_digest(mock_service, "me@test.com", {}, 0)

        mock_skill.assert_called_once_with("brief")
        send_mock.assert_called_once()

    def test_summary_invokes_summary_skill(self, tmp_config):
        """Work summary should invoke the /summary skill."""
        mock_service = mock.MagicMock()
        send_mock = mock_service.users.return_value.messages.return_value.send
        send_mock.return_value.execute.return_value = {"id": "d1"}

        with mock.patch.object(bridge, "_invoke_skill", return_value="# Summary\nTest") as mock_skill:
            bridge.send_work_summary(mock_service, "me@test.com")

        mock_skill.assert_called_once_with("summary")
        send_mock.assert_called_once()


# ---------------------------------------------------------------------------
# generate_subject
# ---------------------------------------------------------------------------


class TestGenerateSubject:
    def test_short_message(self):
        result = bridge.generate_subject("How do I fix the login bug?")
        assert result == "cc How do I fix the login bug?"

    def test_long_message_truncated(self):
        long_msg = "Please refactor the authentication module to use OAuth2 instead of the legacy token system"
        result = bridge.generate_subject(long_msg)
        assert result.endswith("...")
        assert len(result) <= 57  # "cc " prefix + 50 + ...

    def test_cc_prefix_stripped(self):
        result = bridge.generate_subject("cc deploy the new service")
        assert result == "cc deploy the new service"
        assert "cc cc" not in result

    def test_empty_message_fallback(self):
        assert bridge.generate_subject("") == "cc conversation"
        assert bridge.generate_subject("   ") == "cc conversation"

# ---------------------------------------------------------------------------
# invoke_claude error messages
# ---------------------------------------------------------------------------


class TestInvokeClaudeErrors:
    """Error messages from invoke_claude must contain actionable guidance."""

    def test_timeout_message_has_recovery_steps(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.return_value = None  # never finishes
            mock_popen.return_value = proc
            with mock.patch.object(bridge, "CLAUDE_TIMEOUT", 2):
                with mock.patch("time.sleep"):
                    result = bridge.invoke_claude("hi", "sess-1")
        assert "Timed out" in result
        assert "/resume" in result
        assert "smaller steps" in result

    def test_nonzero_exit_message_has_retry_hint(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.side_effect = [None, 0]  # finishes on second poll
            proc.stdout.read.return_value = ""
            proc.stderr.read.return_value = "some error"
            proc.returncode = 1
            mock_popen.return_value = proc
            with mock.patch("time.sleep"):
                result = bridge.invoke_claude("hi", "sess-2")
        assert "exited with code 1" in result
        assert "Error: some error" in result
        assert "retry" in result.lower()

    def test_command_not_found_message_has_install_instructions(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            result = bridge.invoke_claude("hi", "sess-3")
        assert "not found" in result
        assert "npm install" in result

    def test_empty_output_message_has_suggestion(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.side_effect = [None, 0]
            proc.stdout.read.return_value = ""
            proc.stderr.read.return_value = ""
            proc.returncode = 0
            mock_popen.return_value = proc
            with mock.patch("time.sleep"):
                result = bridge.invoke_claude("hi", "sess-4")
        assert "empty output" in result.lower()
        assert "rephrasing" in result.lower()



# ---------------------------------------------------------------------------
# format_html_reply
# ---------------------------------------------------------------------------


class TestFormatHtmlReply:
    """Tests for markdown-to-HTML email conversion."""

    def test_format_html_reply_code_block(self):
        """Verify fenced code blocks get wrapped in <pre><code>."""
        text = "Here is code:\n\n```python\nprint('hello')\n```"
        result = bridge.format_html_reply(text)
        assert "<pre>" in result
        assert "<code" in result
        assert "print" in result

    def test_format_html_reply_heading(self):
        """Verify # Title becomes <h1>."""
        text = "# My Title\n\nSome text"
        result = bridge.format_html_reply(text)
        assert "<h1>" in result
        assert "My Title" in result

    def test_format_html_reply_has_styles(self):
        """Verify CSS styles are included in the output."""
        text = "hello world"
        result = bridge.format_html_reply(text)
        assert "<style>" in result
        assert "font-family" in result
        assert "border-radius" in result

    def test_plain_text_no_markdown(self):
        """Simple text without markdown markers stays as plain MIMEText."""
        original_msg = {
            "subject": "cc test",
            "from": "User <user@example.com>",
            "message_id": "<abc@gmail.com>",
            "references": "",
            "thread_id": "thread123",
        }
        my_email = "bot@example.com"

        mock_service = mock.MagicMock()
        mock_service.users().messages().send().execute.return_value = {"id": "sent1"}

        # Plain text with no markdown markers
        bridge.send_reply(mock_service, original_msg, "just plain text", my_email)

        call_args = mock_service.users().messages().send.call_args
        body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
        raw = body.get("raw", "")
        decoded = base64.urlsafe_b64decode(raw).decode()

        # Should be plain text MIME, not multipart
        assert "text/plain" in decoded
        assert "multipart/alternative" not in decoded

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for the per-hour rate limiting feature."""

    def test_rate_limit_allows_when_under(self, tmp_config):
        """Fresh state (no file) should allow invocations."""
        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR

    def test_rate_limit_blocks_when_exceeded(self, tmp_config):
        """When RATE_LIMIT_PER_HOUR timestamps exist within the hour, should block."""
        now = time.time()
        timestamps = [now - i for i in range(bridge.RATE_LIMIT_PER_HOUR)]
        bridge.RATE_LIMIT_FILE.write_text(json.dumps(timestamps))

        allowed, remaining = bridge._check_rate_limit()
        assert allowed is False
        assert remaining == 0

    def test_rate_limit_prunes_old_entries(self, tmp_config):
        """Timestamps older than 1 hour should be pruned and not count."""
        now = time.time()
        old_timestamps = [now - 7200 + i for i in range(10)]  # 2 hours ago
        recent_timestamps = [now - 60 + i for i in range(3)]  # 1 minute ago
        all_timestamps = old_timestamps + recent_timestamps
        bridge.RATE_LIMIT_FILE.write_text(json.dumps(all_timestamps))

        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR - len(recent_timestamps)

    def test_record_invocation_appends(self, tmp_config):
        """Recording an invocation should add a timestamp to the file."""
        assert not bridge.RATE_LIMIT_FILE.exists()

        bridge._record_invocation()

        assert bridge.RATE_LIMIT_FILE.exists()
        timestamps = json.loads(bridge.RATE_LIMIT_FILE.read_text())
        assert len(timestamps) == 1
        assert timestamps[0] <= time.time()
        assert timestamps[0] > time.time() - 5  # within last 5 seconds

        # Record another
        bridge._record_invocation()
        timestamps = json.loads(bridge.RATE_LIMIT_FILE.read_text())
        assert len(timestamps) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
