#!/usr/bin/env python3
"""Guard against internal information leaking into the public repo.

Run as part of the test suite and as a pre-commit hook.
Scans all tracked files for patterns that should never appear in a public repo.
"""

import re
import subprocess
import sys
from pathlib import Path

# Patterns that must NEVER appear in committed files
FORBIDDEN_PATTERNS = [
    # Company email domains
    (r"@doordash\.com", "DoorDash email address"),
    (r"@deliveroo\.com", "Deliveroo email address"),
    (r"@deliveroo\.co\.uk", "Deliveroo UK email address"),
    # Internal URLs
    (r"doordash\.slack\.com", "DoorDash Slack URL"),
    (r"doordash\.atlassian\.net", "DoorDash Jira/Confluence URL"),
    (r"devconsole\.doordash", "DoorDash DevConsole URL"),
    # Real Slack/API tokens (not placeholder examples)
    (r"xoxb-[0-9]{10,}", "Real Slack bot token"),
    (r"xoxp-[0-9]{10,}", "Real Slack user token"),
    # Hardcoded home directory paths (macOS)
    (r"/Users/[a-zA-Z][a-zA-Z0-9._]+/", "Hardcoded macOS home directory path"),
    # Internal infrastructure
    (r"cell-\d{3}-\d{2}\.cell", "Internal Kubernetes cluster name"),
    (r"dash-management-", "Internal cluster name"),
]

# Files to skip (not source code)
SKIP_FILES = {
    "test_no_secrets.py",  # This file itself contains the patterns as strings
}

# Lines that are allowed exceptions (e.g., placeholder examples in docs)
ALLOWED_EXCEPTIONS = [
    'echo "xoxb-your-token"',       # README placeholder
    "xoxb-your-token-here",         # setup instructions
    "xoxb-...",                     # documentation reference
    "xoxb-fake-token",             # test fixture
    "/Users/jane.doe/",             # generic test example
]


def get_tracked_files() -> list[str]:
    """Get list of git-tracked files."""
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=Path(__file__).parent
    )
    return [f for f in result.stdout.strip().splitlines() if f]


def scan_file(filepath: str) -> list[tuple[int, str, str]]:
    """Scan a single file for forbidden patterns. Returns [(line_num, line, reason)]."""
    if Path(filepath).name in SKIP_FILES:
        return []

    violations = []
    try:
        content = Path(filepath).read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    for line_num, line in enumerate(content.splitlines(), 1):
        # Check if line matches an allowed exception
        if any(exc in line for exc in ALLOWED_EXCEPTIONS):
            continue

        for pattern, reason in FORBIDDEN_PATTERNS:
            if re.search(pattern, line):
                violations.append((line_num, line.strip()[:120], reason))

    return violations


def main() -> int:
    """Scan all tracked files. Returns 0 if clean, 1 if violations found."""
    repo_root = Path(__file__).parent
    files = get_tracked_files()

    all_violations = []
    for filepath in files:
        full_path = repo_root / filepath
        if not full_path.exists():
            continue
        violations = scan_file(str(full_path))
        for line_num, line, reason in violations:
            all_violations.append((filepath, line_num, line, reason))

    if all_violations:
        print(f"\nFOUND {len(all_violations)} SECRET/PII VIOLATION(S):\n")
        for filepath, line_num, line, reason in all_violations:
            print(f"  {filepath}:{line_num} — {reason}")
            print(f"    {line}\n")
        print("Fix these before committing. See test_no_secrets.py for the full pattern list.")
        return 1

    print(f"Scanned {len(files)} files — no secrets or PII found.")
    return 0


# ---------------------------------------------------------------------------
# Pytest integration
# ---------------------------------------------------------------------------

import pytest


class TestNoSecrets:
    """Ensure no internal information leaks into the public repo."""

    def test_no_forbidden_patterns_in_tracked_files(self):
        """All git-tracked files must be free of company emails, tokens, and PII."""
        repo_root = Path(__file__).parent
        files = get_tracked_files()

        violations = []
        for filepath in files:
            full_path = repo_root / filepath
            if not full_path.exists():
                continue
            file_violations = scan_file(str(full_path))
            for line_num, line, reason in file_violations:
                violations.append(f"{filepath}:{line_num} — {reason}: {line}")

        assert violations == [], (
            f"Found {len(violations)} secret/PII violation(s):\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_forbidden_patterns_are_comprehensive(self):
        """Sanity check: our pattern list catches known bad strings."""
        test_cases = [
            ("user@doordash.com", True),
            ("user@deliveroo.com", True),
            ("user@example.com", False),
            ("doordash.slack.com/archives", True),
            ("/Users/john.smith/Projects", True),
            ("xoxb-123456789012-abcdef", True),
            ("xoxb-fake-token", False),  # allowed exception
        ]
        for text, should_match in test_cases:
            matched = False
            if any(exc in text for exc in ALLOWED_EXCEPTIONS):
                matched = False
            else:
                for pattern, _ in FORBIDDEN_PATTERNS:
                    if re.search(pattern, text):
                        matched = True
                        break
            assert matched == should_match, (
                f"Pattern {'should' if should_match else 'should NOT'} match: {text!r}"
            )


if __name__ == "__main__":
    sys.exit(main())
