# Agent installation procedure

Install Coordinator for the user who asked you to follow this file. Perform the work yourself instead
of returning shell commands for the user to run.

1. Clone `https://github.com/alanhoff/agent-coordinator` into a temporary directory and record its
   commit ID.
2. Assemble a fresh directory from the checkout: copy the contents of `skill/`, copy `LICENSE` into
   its root, and copy `src/coordinator/` to `scripts/lib/coordinator/`.
3. Install that complete directory at `~/.agents/skills/coordinator`. Replace an existing destination
   only when its `SKILL.md` declares `name: coordinator`; never merge old and new contents. If the path
   belongs to something else, leave it untouched and ask the user where to install.
4. Remove the temporary checkout and report the installed path and source commit.

Keep role profiles inside `agents/roles/`; do not register them separately. Do not edit Codex settings
or global custom-agent files. Installation ends after placement: Coordinator detects delegation
support when each task runs and uses inline execution when delegation is unavailable.
