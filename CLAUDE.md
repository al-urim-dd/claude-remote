# ClaudeRemote (Al's fork)

## Git workflow
- Work on `main`. This is Al's fork (`al-urim-dd/claude-remote`), not the upstream repo.
- After every commit, push to the fork: `git push fork main`
- Remote `fork` points to `al-urim-dd/claude-remote`. Remote `origin` is the upstream `zhengli-sun/claude-remote`.
- To pull upstream changes: `git fetch origin && git merge origin/main`
- Maintain a draft PR from `al-urim-dd:main` into `zhengli-sun/claude-remote:main`. Currently https://github.com/zhengli-sun/claude-remote/pull/26. After every commit, update the PR body to enumerate all changes on the fork with rationale. Note in the PR that it does not need to land, shared in case useful.
