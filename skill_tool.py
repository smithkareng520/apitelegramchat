# skill_tool.py
"""
技能（skill）工具。

定位
----
skill 是可复用的「能力模板」：每个 skill 由
  - name（机器名）
  - description（一句话说明）
  - system_prompt（注入到当前对话的提示词片段）
  - tools（允许调用的工具白名单，可选）
  - examples（自然语言触发示例，可选）

组成。

内置 7 个开箱即用的技能：
  translator   — 中英互译，自动检测源语言
  summarizer   — 长文摘要，分点输出
  coder        — 工程化代码生成，自带 review 检查
  reviewer     — 代码评审，按安全/性能/可读性三维度
  explainer    — 概念解释，类比+示例
  brainstormer — 头脑风暴，发散后收敛
  planner      — 任务拆解，输出可执行步骤

用户也可以自己注册 custom skill：定义 name + description + prompt + 可选 tools。
custom skill 落在 ./workspace/{chat_id}/skills.json，随 R2 同步，跨会话保留。

操作
----
- list           列出所有可用技能（内置 + 自定义）
- info <name>    查看某个技能的详细定义
- use <name>     应用技能——返回该技能的 system_prompt 片段，AI 据此调整行为
- register       注册自定义技能
- update         更新自定义技能（不允许改内置）
- delete         删除自定义技能
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from workspace_utils import (
    _get_workspace_lock,
    _sync_file_from_r2,
    _sync_file_to_r2,
)

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
SKILLS_FILENAME = "skills.json"
MAX_NAME_LEN = 32
MAX_DESC_LEN = 200
MAX_PROMPT_LEN = 4000
MAX_CUSTOM_SKILLS = 50
NAME_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"  # 必须以字母开头，只允许 a-z 0-9 _

# ---------- 内置技能 ----------
BUILTIN_SKILLS: dict[str, dict] = {
    "translator": {
        "name": "translator",
        "description": "中英互译，自动检测源语言，保留专有名词与代码片段。",
        "system_prompt": (
            "你现在是一个专业的中英互译译者。\n"
            "规则：\n"
            "- 自动检测源语言：中文→译为英文，英文/其他→译为中文。\n"
            "- 保留代码块、URL、人名、专有名词原样，不翻译。\n"
            "- 保留原文的语气和正式度。\n"
            "- 如果用户给了上下文（如「正式场合」「口语化」），按上下文调整。\n"
            "- 输出格式：先给译文，再用 <details> 折叠附上「术语表」（如有专有名词的选择）。\n"
        ),
        "tools": [],
        "examples": ["翻译一下这段话", "把这个翻成英文", "translate this to Chinese"],
        "builtin": True,
    },
    "summarizer": {
        "name": "summarizer",
        "description": "长文摘要：核心观点 + 关键数据 + 行动项，分点输出。",
        "system_prompt": (
            "你现在是一个高效的长文摘要器。\n"
            "规则：\n"
            "- 先用一句话概括全文主旨。\n"
            "- 然后用 <ul> 列出 3-7 个核心观点，每点不超过 30 字。\n"
            "- 如果文中出现具体数字/日期/名称，单独列一个 <details> 折叠的「关键数据」区。\n"
            "- 如果文中有可执行的行动项，再用 <ol> 列出。\n"
            "- 不编造文中没有的信息。\n"
        ),
        "tools": [],
        "examples": ["总结一下", "给我一个摘要", "太长了，提炼要点"],
        "builtin": True,
    },
    "coder": {
        "name": "coder",
        "description": "工程化代码生成：完整、可运行、含错误处理与注释。",
        "system_prompt": (
            "你现在是一个资深的工程化代码工程师。\n"
            "规则：\n"
            "- 优先给出可直接运行的完整代码，不要写省略号占位。\n"
            "- 自动添加类型注解、docstring、必要的错误处理。\n"
            "- 用 <pre><code class=\"language-xxx\"> 输出代码块。\n"
            "- 代码之后用 <details> 折叠「设计说明」：解释关键设计决策、复杂度、边界情况。\n"
            "- 如果用户没指定语言，按问题语境选最合适的（默认 Python）。\n"
            "- 涉及外部依赖时，明确写出 install 命令。\n"
        ),
        "tools": ["bash", "text_editor"],
        "examples": ["写一个脚本", "实现一个函数", "帮我做这个功能"],
        "builtin": True,
    },
    "reviewer": {
        "name": "reviewer",
        "description": "代码评审：按安全 / 性能 / 可读性三维度给意见。",
        "system_prompt": (
            "你现在是一个严格的代码评审专家。\n"
            "规则：\n"
            "- 按「🔴 安全」「🟡 性能」「🟢 可读性」三个维度分组给意见。\n"
            "- 每条意见先指出问题位置（行号或代码片段），再给改进建议。\n"
            "- 用 <table bordered striped> 输出汇总：列 = 维度 / 问题 / 严重度 / 建议。\n"
            "- 末尾给一个总体评分（1-5 星）和最优先修复项。\n"
            "- 不要恭维，直接指出问题。\n"
        ),
        "tools": ["text_editor"],
        "examples": ["帮我 review 这段代码", "看看这个实现有什么问题"],
        "builtin": True,
    },
    "explainer": {
        "name": "explainer",
        "description": "概念解释：用类比 + 示例 + 反例讲清楚。",
        "system_prompt": (
            "你现在是一个擅长深入浅出的概念讲解者。\n"
            "规则：\n"
            "- 先用一句不超过 30 字的话给出定义。\n"
            "- 然后给一个生活化的类比（用 <blockquote> 标出）。\n"
            "- 再用一个最小示例说明（代码或图示，视概念而定）。\n"
            "- 最后用 <details> 折叠「常见误解」：列出 2-3 个初学者容易混淆的点。\n"
            "- 避免堆砌术语；用到术语时立刻就地解释。\n"
        ),
        "tools": [],
        "examples": ["解释一下什么是", "...是啥意思", "能不能讲讲"],
        "builtin": True,
    },
    "brainstormer": {
        "name": "brainstormer",
        "description": "头脑风暴：先发散给 10 个点子，再收敛到 3 个最优解。",
        "system_prompt": (
            "你现在是一个发散思维极强的头脑风暴伙伴。\n"
            "规则：\n"
            "- 第一轮：用 <ol> 给出 10 个尽可能不同的点子，越后期越鼓励跨界组合。\n"
            "- 第二轮：从 10 个里挑出 3 个最优的，用 <table bordered striped> 给出"
            "「点子 / 可行性 / 创新度 / 风险」四列评估。\n"
            "- 末尾用 <blockquote> 给一句行动建议。\n"
            "- 不否定任何点子，先发散再筛选。\n"
        ),
        "tools": [],
        "examples": ["帮我想想", "头脑风暴一下", "有什么好主意"],
        "builtin": True,
    },
    "planner": {
        "name": "planner",
        "description": "任务拆解：把模糊目标拆成可执行步骤，估算工作量。",
        "system_prompt": (
            "你现在是一个项目规划专家。\n"
            "规则：\n"
            "- 先用一句话明确目标（如目标本身模糊，先列出你的假设）。\n"
            "- 把任务拆成 3-7 个阶段，每个阶段用 <h3> 标题。\n"
            "- 每个阶段下用 <ol> 列出具体步骤，每步标注「预计耗时」和「依赖」。\n"
            "- 末尾用 <table bordered striped> 给出汇总：阶段 / 步骤数 / 总耗时 / 关键路径。\n"
            "- 用 <details> 折叠「风险与缓解」。\n"
        ),
        "tools": ["todo"],
        "examples": ["帮我拆解一下这个任务", "怎么规划这个项目", "制定一个计划"],
        "builtin": True,
    },
}


# ---------- 存储层 ----------
def _workspace_path(chat_id: int) -> Path:
    return Path(f"./workspace/{chat_id}").resolve()


def _skills_path(chat_id: int) -> Path:
    return _workspace_path(chat_id) / SKILLS_FILENAME


def _empty_store() -> dict:
    return {"skills": [], "updated_at": 0}


def _load_local(chat_id: int) -> dict:
    path = _skills_path(chat_id)
    if not path.is_file():
        return _empty_store()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            return _empty_store()
        data.setdefault("updated_at", 0)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"skills.json 读取失败 (chat={chat_id}): {e}")
        return _empty_store()


def _save_local(chat_id: int, store: dict) -> None:
    path = _skills_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = int(time.time())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _validate_name(name: str) -> str:
    import re
    name = (name or "").strip().lower()
    if not re.match(NAME_PATTERN, name):
        raise _SkillError(
            f"技能名不合法：必须以字母开头，只允许小写字母/数字/下划线，长度 2-32。得到：{name!r}",
            "bad_name")
    if name in BUILTIN_SKILLS:
        raise _SkillError(f"不能使用内置技能名：{name}", "name_conflict_builtin")
    return name


def _find_custom_skill(skills: list, name: str) -> tuple[int, dict] | None:
    name = (name or "").strip().lower()
    if not name:
        return None
    for i, s in enumerate(skills):
        if s.get("name") == name:
            return i, s
    return None


def _get_skill_payload(store: dict, name: str) -> dict | None:
    """从内置 + 自定义里找一个 skill。"""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name in BUILTIN_SKILLS:
        return dict(BUILTIN_SKILLS[name])
    found = _find_custom_skill(store.get("skills", []), name)
    if found:
        return dict(found[1])
    return None


class _SkillError(Exception):
    def __init__(self, message: str, code: str = "skill_error"):
        super().__init__(message)
        self.message = message
        self.code = code


async def _mutate(chat_id: int, fn) -> dict:
    """
    性能优化：只同步 skills.json 单个文件（而非全量 workspace）。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await _sync_file_from_r2(chat_id, SKILLS_FILENAME)
        except Exception as e:
            logger.warning(f"skill: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            store, payload = fn(store)
        except _SkillError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        _save_local(chat_id, store)
        try:
            await _sync_file_to_r2(chat_id, SKILLS_FILENAME)
        except Exception as e:
            logger.warning(f"skill: local→R2 同步失败 (chat={chat_id}): {e}")
        return payload


# ---------- 业务逻辑 ----------
def _op_list(store: dict) -> dict:
    custom = store.get("skills", [])
    all_skills = list(BUILTIN_SKILLS.values()) + list(custom)
    summaries = [{
        "name": s.get("name"),
        "description": s.get("description", ""),
        "builtin": bool(s.get("builtin")),
        "tools": list(s.get("tools", []) or []),
    } for s in all_skills]
    return store, {
        "ok": True,
        "action": "list",
        "skills": summaries,
        "total": len(summaries),
        "builtin_count": len(BUILTIN_SKILLS),
        "custom_count": len(custom),
    }


def _op_info(store: dict, name: str) -> dict:
    s = _get_skill_payload(store, name)
    if not s:
        raise _SkillError(f"找不到技能：{name}", "not_found")
    return store, {"ok": True, "action": "info", "skill": _skill_summary(s)}


def _op_use(store: dict, name: str) -> dict:
    """应用技能：返回 system_prompt + tools 白名单，让 AI 据此调整行为。"""
    s = _get_skill_payload(store, name)
    if not s:
        raise _SkillError(f"找不到技能：{name}", "not_found")
    return store, {
        "ok": True,
        "action": "use",
        "skill": _skill_summary(s),
        "system_prompt": s.get("system_prompt", ""),
        "tools": list(s.get("tools", []) or []),
        "instruction": (
            f"已激活技能 {s.get('name')}。请在接下来的回复中遵循该技能的 system_prompt，"
            "直到用户切换或取消。"
        ),
    }


def _op_register(store: dict, name: str, description: str, system_prompt: str,
                 tools: Any, examples: Any) -> dict:
    name = _validate_name(name)
    description = (description or "").strip()
    system_prompt = (system_prompt or "").strip()
    if not description:
        raise _SkillError("description 不能为空", "empty_desc")
    if not system_prompt:
        raise _SkillError("system_prompt 不能为空", "empty_prompt")
    if len(description) > MAX_DESC_LEN:
        description = description[:MAX_DESC_LEN]
    if len(system_prompt) > MAX_PROMPT_LEN:
        system_prompt = system_prompt[:MAX_PROMPT_LEN]
    tools_list = _normalize_tools(tools)
    examples_list = _normalize_examples(examples)

    if _find_custom_skill(store.get("skills", []), name):
        raise _SkillError(f"自定义技能 {name} 已存在；如需更新请用 update", "exists")
    if len(store.get("skills", [])) >= MAX_CUSTOM_SKILLS:
        raise _SkillError(f"自定义技能数量已达上限 {MAX_CUSTOM_SKILLS}", "too_many")

    skill = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "tools": tools_list,
        "examples": examples_list,
        "builtin": False,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "id": uuid.uuid4().hex[:8],
    }
    store.setdefault("skills", []).append(skill)
    return store, {
        "ok": True,
        "action": "register",
        "skill": _skill_brief(skill),
        "total_custom": len(store["skills"]),
    }


def _op_update(store: dict, name: str, description: Optional[str],
               system_prompt: Optional[str], tools: Any, examples: Any) -> dict:
    name = _validate_name(name)
    found = _find_custom_skill(store.get("skills", []), name)
    if not found:
        raise _SkillError(f"找不到自定义技能：{name}（内置技能不可更新）", "not_found")
    _, skill = found
    changed = []
    if description is not None:
        d = description.strip()
        if not d:
            raise _SkillError("description 不能为空", "empty_desc")
        skill["description"] = d[:MAX_DESC_LEN]
        changed.append("description")
    if system_prompt is not None:
        p = system_prompt.strip()
        if not p:
            raise _SkillError("system_prompt 不能为空", "empty_prompt")
        skill["system_prompt"] = p[:MAX_PROMPT_LEN]
        changed.append("system_prompt")
    if tools is not None:
        skill["tools"] = _normalize_tools(tools)
        changed.append("tools")
    if examples is not None:
        skill["examples"] = _normalize_examples(examples)
        changed.append("examples")
    if changed:
        skill["updated_at"] = int(time.time())
    return store, {
        "ok": True,
        "action": "update",
        "skill": _skill_brief(skill),
        "changed": changed,
    }


def _op_delete(store: dict, name: str) -> dict:
    name = (name or "").strip().lower()
    if name in BUILTIN_SKILLS:
        raise _SkillError(f"内置技能 {name} 不可删除", "builtin_protected")
    found = _find_custom_skill(store.get("skills", []), name)
    if not found:
        raise _SkillError(f"找不到自定义技能：{name}", "not_found")
    idx, skill = found
    store["skills"].pop(idx)
    return store, {
        "ok": True,
        "action": "delete",
        "skill": _skill_brief(skill),
        "total_custom": len(store.get("skills", [])),
    }


def _normalize_tools(tools: Any) -> list[str]:
    if tools is None:
        return []
    if isinstance(tools, str):
        parts = [t.strip() for t in tools.replace(",", " ").split() if t.strip()]
    elif isinstance(tools, list):
        parts = [str(t).strip() for t in tools if str(t).strip()]
    else:
        return []
    # 去重 + 限长
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p[:32])
        if len(out) >= 16:
            break
    return out


def _normalize_examples(examples: Any) -> list[str]:
    if examples is None:
        return []
    if isinstance(examples, str):
        parts = [e.strip() for e in examples.split("|") if e.strip()]
    elif isinstance(examples, list):
        parts = [str(e).strip() for e in examples if str(e).strip()]
    else:
        return []
    return [e[:80] for e in parts[:10]]


def _skill_summary(s: dict) -> dict:
    """完整摘要（含 system_prompt）——仅用于 info / use 动作。"""
    return {
        "name": s.get("name"),
        "description": s.get("description", ""),
        "builtin": bool(s.get("builtin")),
        "tools": list(s.get("tools", []) or []),
        "examples": list(s.get("examples", []) or []),
        "system_prompt": s.get("system_prompt", ""),
        "id": s.get("id"),
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
    }


def _skill_brief(s: dict) -> dict:
    """精简摘要（不含 system_prompt）——用于 register / update / delete / list。
    避免把 LLM 刚写的 system_prompt 原样回传，浪费 token 且可能让模型困惑。
    """
    return {
        "name": s.get("name"),
        "description": s.get("description", ""),
        "builtin": bool(s.get("builtin")),
        "tools": list(s.get("tools", []) or []),
        "examples": list(s.get("examples", []) or []),
        "id": s.get("id"),
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
    }


# ---------- 工具入口 ----------
async def execute_skill(
    chat_id: int,
    action: str = "list",
    name: Optional[str] = None,
    description: Optional[str] = None,
    system_prompt: Optional[str] = None,
    tools: Any = None,
    examples: Any = None,
) -> str:
    """
    skill 工具主入口。返回 JSON 字符串。
    action: list | info | use | register | update | delete
    """
    action = (action or "list").strip().lower()

    if action == "list":
        return json.dumps(await _mutate(chat_id, _op_list), ensure_ascii=False)
    if action == "info":
        return json.dumps(await _mutate(chat_id, lambda s: _op_info(s, name or "")),
                          ensure_ascii=False)
    if action == "use":
        return json.dumps(await _mutate(chat_id, lambda s: _op_use(s, name or "")),
                          ensure_ascii=False)
    if action == "register":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_register(s, name or "", description or "",
                                                          system_prompt or "", tools, examples)),
            ensure_ascii=False,
        )
    if action == "update":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_update(s, name or "", description,
                                                        system_prompt, tools, examples)),
            ensure_ascii=False,
        )
    if action == "delete":
        return json.dumps(await _mutate(chat_id, lambda s: _op_delete(s, name or "")),
                          ensure_ascii=False)

    return json.dumps({"ok": False, "error": f"未知 action: {action}", "code": "bad_action"},
                      ensure_ascii=False)


# ---------- 富文本渲染 ----------
def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_skill_card(payload: dict, max_items: int = 30) -> str:
    if not isinstance(payload, dict):
        return f"<p>{_esc(payload)}</p>"
    if not payload.get("ok"):
        return (f"<p>❌ <b>技能操作失败</b></p>"
                f"<p>{_esc(payload.get('error', '未知错误'))}</p>")

    action = payload.get("action", "list")
    if action == "list":
        skills = payload.get("skills", []) or []
        header = "<h3>🎯 可用技能</h3>"
        stat = (f"共 <b>{payload.get('total', 0)}</b> 个 · "
                f"内置 <b>{payload.get('builtin_count', 0)}</b> · "
                f"自定义 <b>{payload.get('custom_count', 0)}</b>")
        if not skills:
            return header + f"<p>{stat}</p><blockquote>暂无可用技能</blockquote>"
        rows = []
        for s in skills[:max_items]:
            tag = "🧩 内置" if s.get("builtin") else "⭐ 自定义"
            tools = s.get("tools", []) or []
            tools_html = " · ".join(f"<code>{_esc(t)}</code>" for t in tools) if tools else "<i>无工具</i>"
            rows.append(
                f"<tr><td><b>{_esc(s.get('name'))}</b></td>"
                f"<td>{tag}</td>"
                f"<td>{_esc(s.get('description'))}</td>"
                f"<td>{tools_html}</td></tr>"
            )
        table = (
            "<table bordered striped>"
            "<tr><th>技能</th><th>类型</th><th>说明</th><th>可用工具</th></tr>"
            + "".join(rows)
            + "</table>"
        )
        extra = len(skills) - max_items
        extra_html = f"<p><i>… 还有 {extra} 个未显示</i></p>" if extra > 0 else ""
        tip = ("<p><i>调用 skill use &lt;name&gt; 激活某个技能，"
               "或 skill register 创建自定义技能。</i></p>")
        return header + f"<p>{stat}</p>" + table + extra_html + tip

    if action in ("info", "use"):
        s = payload.get("skill", {})
        title_emoji = "🧩" if s.get("builtin") else "⭐"
        header = f"<h3>{title_emoji} 技能：{_esc(s.get('name'))}</h3>"
        desc = f"<p>{_esc(s.get('description'))}</p>"
        tools = s.get("tools", []) or []
        tools_html = ("<p>可用工具：" + " ".join(f"<code>{_esc(t)}</code>" for t in tools) + "</p>"
                      if tools else "<p>可用工具：<i>无（仅对话）</i></p>")
        examples = s.get("examples", []) or []
        ex_html = ""
        if examples:
            ex_html = "<p>触发示例：</p><ul>" + "".join(f"<li>{_esc(e)}</li>" for e in examples) + "</ul>"
        prompt_html = ""
        if payload.get("system_prompt") or s.get("system_prompt"):
            prompt = payload.get("system_prompt") or s.get("system_prompt")
            prompt_html = (
                "<details><summary>系统提示词（点击展开）</summary>"
                f"<pre><code>{_esc(prompt)}</code></pre>"
                "</details>"
            )
        instruction = ""
        if action == "use" and payload.get("instruction"):
            instruction = f"<blockquote>{_esc(payload['instruction'])}</blockquote>"
        return header + desc + tools_html + ex_html + prompt_html + instruction

    if action == "register":
        s = payload.get("skill", {})
        return (
            f"<p>⭐ <b>已注册自定义技能</b> <code>{_esc(s.get('name'))}</code></p>"
            f"<p>{_esc(s.get('description'))}</p>"
            f"<p><i>当前共 {payload.get('total_custom', 0)} 个自定义技能</i></p>"
        )
    if action == "update":
        s = payload.get("skill", {})
        return (
            f"<p>📝 <b>已更新技能</b> <code>{_esc(s.get('name'))}</code></p>"
            f"<p>{_esc(s.get('description'))}</p>"
            f"<p><i>修改字段：{', '.join(payload.get('changed', [])) or '无'}</i></p>"
        )
    if action == "delete":
        s = payload.get("skill", {})
        return (
            f"<p>🗑️ <b>已删除技能</b> <code>{_esc(s.get('name'))}</code></p>"
            f"<p><i>剩余 {payload.get('total_custom', 0)} 个自定义技能</i></p>"
        )
    return f"<p>{_esc(payload)}</p>"


# ---------- 工具定义（OpenAI function-calling schema） ----------
# 注意：description 字段是给 AI 阅读的「工具说明书」，全部用纯文本，
# 不使用 Markdown 语法，与系统提示词风格保持一致。
SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Skill registry — apply a reusable capability template (translator / summarizer / coder / reviewer / explainer / brainstormer / planner, or any user-defined custom skill). Each skill bundles a system_prompt and an optional tool whitelist. 6 actions: list / info / use / register / update / delete. Use 'use' to activate a skill — its system_prompt will guide your behavior until the user switches or cancels. Use 'register' to let the user define their own skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次操作目的（≤60字）。示例：激活 summarizer 技能"
                },
                "action": {
                    "type": "string",
                    "enum": ["list", "info", "use", "register", "update", "delete"],
                    "description": "要执行的操作。默认 list。"
                },
                "name": {
                    "type": "string",
                    "description": "技能名称。小写字母/数字/下划线，首字母必须是字母，长度 2-32。info/use/register/update/delete 必填。"
                },
                "description": {
                    "type": "string",
                    "description": "一句话描述（仅 register/update）。最长 200 字符。"
                },
                "system_prompt": {
                    "type": "string",
                    "description": "技能的 system_prompt 片段（仅 register/update）。最长 4000 字符。"
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的工具白名单（仅 register/update）。如 [bash, text_editor]。空数组表示不允许任何工具。"
                },
                "examples": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的触发示例话术（仅 register/update）。最多 10 条，每条 ≤80 字符。"
                }
            },
            "required": ["action"]
        },
        "input_examples": [
            {"action": "list"},
            {"action": "use", "name": "translator"},
            {"action": "info", "name": "coder"},
            {"action": "register", "name": "my_writer", "description": "我的写作助手",
             "system_prompt": "你是一个温柔的写作助手，输出总是带诗意。", "tools": []},
            {"action": "update", "name": "my_writer", "description": "更新后的描述"},
            {"action": "delete", "name": "my_writer"}
        ]
    }
}
