---
name: universal-workflow
description: General-purpose on-demand skill for discovering the right skill, selecting the smallest tool, and executing multi-step work safely.
effort: medium
priority: 100
allowed-tools:
  - skill.catalog
  - skill.read
  - skill.activate
  - search.web
  - search.fetch
  - subagent.run
  - shell.exec
  - workspace.present
  - todo.manage
  - memory.manage
---

# Universal Workflow Skill

Use this skill when the task is broad, multi-step, or benefits from choosing the right skill before acting.

## What to do
1. Inspect the skill catalog when the best route is not obvious.
2. Activate the smallest skill that actually matches the task.
3. Read only the selected skill body before doing work.
4. Batch independent tool calls together; keep dependent calls serial.
5. Prefer deterministic tools, workspace files, todos, and memory over repeating context in chat.
6. Use a subagent only for a clearly isolated subproblem.

## Guardrails
- Do not load or execute extra skills “just in case”.
- Do not invent results, file contents, or tool output.
- Report failures, missing inputs, and permission limits plainly.
- Keep the final response concise and grounded in the available evidence.

## Output style
- Lead with the answer or the next concrete step.
- Use short, direct sentences.
- Include links, file paths, or tool results only when they help the user act.
