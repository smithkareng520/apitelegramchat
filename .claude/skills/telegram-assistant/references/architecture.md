# Telegram Assistant Skill Architecture

This skill is file-based. The markdown file is the source of truth for the agent-facing instruction text.
At startup, the platform sees only each skill's metadata (`name` and `description`). When a task matches, it reads the matching `SKILL.md` from the filesystem, and then loads any additional files only if needed.
