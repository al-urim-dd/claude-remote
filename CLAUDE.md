# ClaudeRemote (Al's fork)

## Git workflow
- Work on `main`. This is Al's fork (`al-urim-dd/claude-remote`), not the upstream repo.
- After every commit, push to the fork: `git push fork main`
- Remote `fork` points to `al-urim-dd/claude-remote`. Remote `origin` is the upstream `zhengli-sun/claude-remote`.
- To pull upstream changes: `git fetch origin && git merge origin/main`
- Maintain a draft PR from `al-urim-dd:main` into `zhengli-sun/claude-remote:main`. Currently https://github.com/zhengli-sun/claude-remote/pull/26. After every commit, update the PR body to enumerate all changes on the fork with rationale. Note in the PR that it does not need to land, shared in case useful.

## Post-change verification (REQUIRED after any bridge.py change)
After every commit that touches `bridge.py`, `requirements.txt`, or `.env.example`, you MUST run:

```
./scripts/redeploy.sh 300
```

This stops and restarts the bridge, then monitors the log for 5 minutes. It filters out known-benign lines (retried 429s, `missing_scope` on reactions, deferred-mention log, bot-invite attempts) and flags anything else.

- Exit 0 + `clean window - no anomalies` → done, move on.
- Exit 1 → anomalies printed to stderr. Read them, diagnose root cause, fix, commit, re-run `redeploy.sh`. Repeat until exit 0.
- Exit 2 → bridge failed to start at all. Check startup banner / stack trace in `~/.claude-remote/bridge.log`.

Short variants for faster iteration: `./scripts/redeploy.sh 60` for a 1-minute smoke, `./scripts/redeploy.sh 0` to restart only with no monitor.

Prefer `run_in_background: true` for the 300s run so the agent can work in parallel and pick up the report when it completes.
