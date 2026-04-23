"""Unit tests for ClaudeRemote Slack Bridge (now part of unified bridge module)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

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
        GMAIL_PROCESSED_FILE=tmp_path / "processed.txt",
        SESSIONS_FILE=tmp_path / "thread_sessions.json",
        PID_FILE=tmp_path / "bridge.pid",
        LOG_FILE=tmp_path / "bridge.log",
        RATE_LIMIT_FILE=tmp_path / "rate_limit.json",
        CANCEL_FILE=tmp_path / "cancel.txt",
        SLACK_STATE_FILE=tmp_path / "slack_agent_state.json",
        CREDENTIALS_FILE=tmp_path / "credentials.json",
    ):
        yield tmp_path


# ---------------------------------------------------------------------------
# Slack message parsing and filtering
# ---------------------------------------------------------------------------


class TestSlackMessageParsing:
    """Tests for MCP-based message parsing."""

    def test_parse_new_messages_basic(self):
        channel_text = (
            "=== Message from user1 (U123)\n"
            "Message TS: 1000.001\n"
            "Hello world\n"
            "\n"
            "=== Message from user2 (U456)\n"
            "Message TS: 1000.002\n"
            "Second message\n"
        )
        messages = bridge.parse_new_messages(channel_text, "999.000")
        assert len(messages) == 2
        assert messages[0]["user"] == "U123"
        assert messages[0]["ts"] == "1000.001"
        assert messages[1]["user"] == "U456"

    def test_parse_new_messages_filters_old(self):
        channel_text = (
            "=== Message from user1 (U123)\n"
            "Message TS: 900.001\n"
            "Old message\n"
            "\n"
            "=== Message from user2 (U456)\n"
            "Message TS: 1100.002\n"
            "New message\n"
        )
        messages = bridge.parse_new_messages(channel_text, "1000.000")
        assert len(messages) == 1
        assert messages[0]["ts"] == "1100.002"

    def test_parse_thread_replies_basic(self):
        thread_text = (
            "=== Original message ===\n"
            "Some original\n"
            "\n"
            "THREAD REPLIES\n"
            "\n"
            "--- Reply 1 ---\n"
            "From: user1 (U123)\n"
            "Message TS: 1000.005\n"
            "Reply text here\n"
        )
        replies = bridge.parse_thread_replies(thread_text, "999.000")
        assert len(replies) == 1
        assert replies[0]["user"] == "U123"
        assert "Reply text here" in replies[0]["text"]

    def test_parse_thread_replies_filters_old(self):
        thread_text = (
            "THREAD REPLIES\n"
            "--- Reply 1 ---\n"
            "From: user1 (U123)\n"
            "Message TS: 900.005\n"
            "Old reply\n"
            "--- Reply 2 ---\n"
            "From: user2 (U456)\n"
            "Message TS: 1100.005\n"
            "New reply\n"
        )
        replies = bridge.parse_thread_replies(thread_text, "1000.000")
        assert len(replies) == 1
        assert replies[0]["ts"] == "1100.005"


class TestShouldProcess:
    """Tests for should_process filter."""

    def test_normal_message(self):
        assert bridge.should_process({"text": "hello"}) is True

    def test_agent_prefix_skipped(self):
        assert bridge.should_process({"text": f"{bridge.AGENT_PREFIX} response"}) is False

    def test_empty_skipped(self):
        assert bridge.should_process({"text": ""}) is False

    def test_whitespace_skipped(self):
        assert bridge.should_process({"text": "   "}) is False

    def test_join_message_skipped(self):
        assert bridge.should_process({"text": "user has joined the channel"}) is False

    def test_claude_sent_skipped(self):
        assert bridge.should_process({"text": "Sent using Claude"}) is False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for rate limiting Claude invocations."""

    def test_allowed_when_empty(self, tmp_config):
        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR

    def test_allowed_after_one_invocation(self, tmp_config):
        bridge._record_invocation()
        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR - 1

    def test_blocked_at_limit(self, tmp_config):
        """Should be blocked after RATE_LIMIT_PER_HOUR invocations in an hour."""
        now = time.time()
        timestamps = [now - i for i in range(bridge.RATE_LIMIT_PER_HOUR)]
        bridge.RATE_LIMIT_FILE.write_text(json.dumps(timestamps))

        allowed, remaining = bridge._check_rate_limit()
        assert allowed is False
        assert remaining == 0

    def test_old_entries_pruned(self, tmp_config):
        """Entries older than 1 hour should be pruned."""
        now = time.time()
        old_timestamps = [now - 3700 for _ in range(bridge.RATE_LIMIT_PER_HOUR)]
        bridge.RATE_LIMIT_FILE.write_text(json.dumps(old_timestamps))

        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR

    def test_corrupt_json_handled(self, tmp_config):
        bridge.RATE_LIMIT_FILE.write_text("not json")
        allowed, remaining = bridge._check_rate_limit()
        assert allowed is True
        assert remaining == bridge.RATE_LIMIT_PER_HOUR


# ---------------------------------------------------------------------------
# Slack state management
# ---------------------------------------------------------------------------


class TestSlackState:
    """Tests for Slack state file management."""

    def test_empty_state(self, tmp_config):
        state = bridge.load_slack_state()
        assert state["channel_id"] == ""
        assert state["active_threads"] == {}

    def test_state_roundtrip(self, tmp_config):
        state = {
            "channel_id": "C123",
            "channel_name": "test",
            "last_checked_ts": "1000.0",
            "active_threads": {"1000.1": "1000.2"},
        }
        bridge.save_slack_state(state)
        loaded = bridge.load_slack_state()
        assert loaded["channel_id"] == "C123"
        assert loaded["active_threads"]["1000.1"] == "1000.2"

    def test_corrupt_json_returns_default(self, tmp_config):
        bridge.SLACK_STATE_FILE.write_text("not json")
        state = bridge.load_slack_state()
        assert state["channel_id"] == ""


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TestTokenManagement:
    """Tests for Slack MCP token loading."""

    def test_missing_credentials_file(self, tmp_config):
        result = bridge._load_credentials()
        assert result == {}

    def test_find_token_entry_found(self):
        creds = {
            "mcpOAuth": {
                "slack-mcp-key": {
                    "serverUrl": "https://mcp.slack.com/mcp",
                    "accessToken": "xoxe.xoxp-test-token",
                    "expiresAt": int(time.time() * 1000) + 3600000,
                }
            }
        }
        entry = bridge._find_slack_token_entry(creds)
        assert entry is not None
        assert entry["accessToken"] == "xoxe.xoxp-test-token"

    def test_find_token_entry_not_found(self):
        creds = {"mcpOAuth": {"other-key": {"serverUrl": "https://example.com", "accessToken": "abc"}}}
        entry = bridge._find_slack_token_entry(creds)
        assert entry is None

    def test_expired_token_returns_none(self, tmp_config):
        creds = {
            "mcpOAuth": {
                "slack-mcp-key": {
                    "serverUrl": "https://mcp.slack.com/mcp",
                    "accessToken": "xoxe.xoxp-test-token",
                    "expiresAt": int(time.time() * 1000) - 3600000,
                }
            }
        }
        bridge.CREDENTIALS_FILE.write_text(json.dumps(creds))
        token = bridge.get_slack_token()
        assert token is None

    def test_valid_token_returned(self, tmp_config):
        creds = {
            "mcpOAuth": {
                "slack-mcp-key": {
                    "serverUrl": "https://mcp.slack.com/mcp",
                    "accessToken": "xoxe.xoxp-valid-token",
                    "expiresAt": int(time.time() * 1000) + 3600000,
                }
            }
        }
        bridge.CREDENTIALS_FILE.write_text(json.dumps(creds))
        token = bridge.get_slack_token()
        assert token == "xoxe.xoxp-valid-token"


# ---------------------------------------------------------------------------
# MCP text extraction
# ---------------------------------------------------------------------------


class TestMcpTextExtraction:
    """Tests for extracting text from MCP responses."""

    def test_extract_with_messages_key(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"messages": "Hello world"})}
            ]
        }
        assert bridge._extract_mcp_text(result) == "Hello world"

    def test_extract_raw_fallback(self):
        result = {
            "content": [
                {"type": "text", "text": "plain text"}
            ]
        }
        assert bridge._extract_mcp_text(result) == "plain text"

    def test_extract_none_for_empty(self):
        assert bridge._extract_mcp_text({}) is None

    def test_extract_none_for_none(self):
        assert bridge._extract_mcp_text(None) is None

    def test_extract_none_for_no_text_type(self):
        result = {"content": [{"type": "image", "data": "abc"}]}
        assert bridge._extract_mcp_text(result) is None


# ---------------------------------------------------------------------------
# Cancel support
# ---------------------------------------------------------------------------


class TestCancelSupport:
    """Tests for the cancel mechanism."""

    def test_check_cancel_no_file(self, tmp_config):
        assert bridge._check_cancel("thread-1") is False

    def test_check_cancel_thread_found(self, tmp_config):
        bridge.CANCEL_FILE.write_text("thread-1\n")
        assert bridge._check_cancel("thread-1") is True
        # Should be removed after checking
        if bridge.CANCEL_FILE.exists():
            content = bridge.CANCEL_FILE.read_text()
            assert "thread-1" not in content

    def test_check_cancel_thread_not_found(self, tmp_config):
        bridge.CANCEL_FILE.write_text("thread-2\n")
        assert bridge._check_cancel("thread-1") is False


# ---------------------------------------------------------------------------
# invoke_claude error messages
# ---------------------------------------------------------------------------


class TestInvokeClaudeErrors:
    """Error messages from invoke_claude must contain actionable guidance."""

    def test_timeout_message_has_recovery_steps(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.return_value = None
            mock_popen.return_value = proc
            with mock.patch.object(bridge, "CLAUDE_TIMEOUT", 2):
                with mock.patch("time.sleep"):
                    result = bridge.invoke_claude("hi", "sess-1")
        assert "Timed out" in result
        assert "/resume" in result

    def test_command_not_found_message(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            result = bridge.invoke_claude("hi", "sess-3")
        assert "not found" in result
        assert "npm install" in result

    def test_empty_output_message(self):
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


# ---------------------------------------------------------------------------
# Business hours
# ---------------------------------------------------------------------------


class TestBusinessHours:
    """Tests for business hours gating."""

    def test_disabled_always_true(self):
        with mock.patch.object(bridge, "BUSINESS_HOURS_ONLY", False):
            assert bridge.is_business_hours() is True

    def test_within_hours(self):
        with mock.patch.object(bridge, "BUSINESS_HOURS_ONLY", True), \
             mock.patch.object(bridge, "BUSINESS_HOURS_START", 8), \
             mock.patch.object(bridge, "BUSINESS_HOURS_END", 22):
            with mock.patch("bridge.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 7, 12, 0, 0)
                assert bridge.is_business_hours() is True

    def test_outside_hours(self):
        with mock.patch.object(bridge, "BUSINESS_HOURS_ONLY", True), \
             mock.patch.object(bridge, "BUSINESS_HOURS_START", 8), \
             mock.patch.object(bridge, "BUSINESS_HOURS_END", 22):
            with mock.patch("bridge.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 7, 3, 0, 0)
                assert bridge.is_business_hours() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
