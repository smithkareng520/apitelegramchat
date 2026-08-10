from __future__ import annotations

import json
from typing import Any

from apitelegramchat.skill_tool import execute_skill
from .registry import _chat_id, _skill_prompt_text


async def list_prompts() -> list[dict[str, Any]]:
    raw = await execute_skill(_chat_id(), action="list")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"ok": False, "skills": []}
    prompts: list[dict[str, Any]] = []
    for skill in payload.get("skills", []) or []:
        prompts.append({
            "name": f"skill.{skill.get('name')}",
            "description": skill.get("description", ""),
            "arguments": [{"name": "name", "description": "Skill name", "required": True}],
        })
    return prompts


async def get_prompt(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not name.startswith("skill."):
        return {"error": {"code": -32601, "message": f"Unknown prompt: {name}"}}
    skill_name = name.split(".", 1)[1]
    raw = await execute_skill(_chat_id(), action="use", name=skill_name)
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"ok": False, "error": "invalid response"}
    if not payload.get("ok"):
        return {"error": {"code": -32000, "message": payload.get("error", "unknown")}}
    return {
        "description": payload.get("instruction", skill_name),
        "messages": [{"role": "system", "content": {"type": "text", "text": _skill_prompt_text(payload)}}],
    }
