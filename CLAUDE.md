# ClaudeRemote (Al's fork)

## Git workflow
- Always work on the `al-claude-remote` branch. Never commit directly to `main`.
- After every commit, push to the fork: `git push fork al-claude-remote`
- Remote `fork` points to `al-urim-dd/claude-remote`. Remote `origin` is the upstream `zhengli-sun/claude-remote`.
- Maintain a draft PR from `al-claude-remote` into `main` on the fork. If the PR doesn't exist yet, create it with:
  ```
  gh pr create --repo al-urim-dd/claude-remote --base main --head al-claude-remote --draft \
    --title "Al's local config and customizations"
  ```
  Update the PR body to reflect all changes made so far. Note that this PR does not need to land, it is shared in case changes are useful upstream.
