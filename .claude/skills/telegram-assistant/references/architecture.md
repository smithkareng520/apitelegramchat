# Telegram Assistant Skill Architecture

This skill is file-based. The markdown file is the source of truth for the agent-facing instruction text.
The runtime reads `.claude/skills/**/SKILL.md` and exposes them through `skill.manage` and MCP prompts.
