#!/usr/bin/env bash
#
# Regenerate PR #26 description on zhengli-sun/claude-remote from commit history.
# Called by Claude Code PostToolUse hook after git push.
#
set -euo pipefail

UPSTREAM_REPO="zhengli-sun/claude-remote"
PR_NUMBER=26

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found, skipping PR update"; exit 0; }

git fetch origin --quiet 2>/dev/null || true

COMMITS=$(git log origin/main..HEAD --reverse --format='%H')

if [ -z "$COMMITS" ]; then
    echo "No commits ahead of origin/main, skipping PR update"
    exit 0
fi

ITEM_NUM=0
CHANGES=""

while IFS= read -r sha; do
    ITEM_NUM=$((ITEM_NUM + 1))
    SUBJECT=$(git log -1 --format='%s' "$sha")
    BODY=$(git log -1 --format='%b' "$sha" | sed '/^$/d')

    if [ -n "$BODY" ]; then
        DESC=$(echo "$BODY" | awk '/^$/{exit} {print}' | tr '\n' ' ' | sed 's/  */ /g' | sed 's/ *$//')
        CHANGES="${CHANGES}${ITEM_NUM}. **${SUBJECT}** — ${DESC}
"
    else
        CHANGES="${CHANGES}${ITEM_NUM}. **${SUBJECT}**
"
    fi
done <<< "$COMMITS"

PR_BODY="## Summary

Al's fork with incremental fixes and improvements. Does not need to land, shared in case useful.

## Changes on fork

${CHANGES}
🤖 Generated with [Claude Code](https://claude.com/claude-code)"

gh pr edit "$PR_NUMBER" --repo "$UPSTREAM_REPO" --body "$PR_BODY"
echo "Updated PR #${PR_NUMBER} on ${UPSTREAM_REPO}"
