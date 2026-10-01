#!/usr/bin/env python3
"""探测 OpenAI-compatible 网关对 Responses 工具续轮的真实支持情况。

用法：
  BASE_URL=https://ai.lfree.org/bot/XXXX/v1 API_KEY=sk-... MODEL=muse-spark-1.3-contributor \
  python scripts/probe_responses_gateway.py

三组对照（同一个工具调用，流式，store=true）：
  A 官方标准      previous_response_id + input=[function_call_output]
  B 无链全量重放   无 previous_response_id，input=[user, function_call, function_call_output]
  C 链 + 完整配对  previous_response_id + input=[function_call, function_call_output]
结论判读：A 失败且 B 成功 => 网关不支持“链 + 孤立 function_call_output”（与本项目代码无关）；
A 成功 => 问题在本项目请求之外的某处，把 A 的 HTTP 响应体贴出来继续查。
"""
import json, os, sys
import httpx

BASE = os.environ["BASE_URL"].rstrip("/")
KEY = os.environ["API_KEY"]
MODEL = os.environ["MODEL"]
TOOL = {"type": "function", "name": "get_time", "description": "返回当前时间",
        "parameters": {"type": "object", "properties": {}, "required": []}}
USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "现在几点？请调用 get_time 工具回答。"}]}


def post(body):
    body = {"model": MODEL, "stream": True, "store": True, "max_output_tokens": 512, **body}
    events, status, err = [], None, ""
    with httpx.stream("POST", f"{BASE}/responses", json=body, timeout=120,
                      headers={"Authorization": f"Bearer {KEY}"}) as r:
        status = r.status_code
        if status >= 400:
            err = r.read().decode("utf-8", "replace")[:800]
            return status, err, None
        for line in r.iter_lines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
    done = next((e["response"] for e in events if e.get("type") == "response.completed"), None)
    return status, err, done


st, err, r1 = post({"input": [USER], "tools": [TOOL], "tool_choice": "required"})
if not r1:
    sys.exit(f"首个请求失败 status={st} {err}")
fc = next((i for i in r1.get("output", []) if i.get("type") == "function_call"), None)
if not fc:
    sys.exit(f"模型未产生 function_call：{json.dumps(r1.get('output'), ensure_ascii=False)[:500]}")
out = {"type": "function_call_output", "call_id": fc["call_id"], "output": "2026-10-01 01:00:00"}
fc_in = {k: fc[k] for k in ("type", "call_id", "name", "arguments") if k in fc}
print(f"response_id={r1['id']} call_id={fc['call_id']}\n")

cases = {
    "A 链 + 孤立 function_call_output（官方标准）": {"previous_response_id": r1["id"], "input": [out], "tools": [TOOL]},
    "B 无链全量重放": {"input": [USER, fc_in, out], "tools": [TOOL]},
    "C 链 + function_call + function_call_output": {"previous_response_id": r1["id"], "input": [fc_in, out], "tools": [TOOL]},
}
for name, body in cases.items():
    st, err, done = post(body)
    print(f"{name}: HTTP {st}" + (f"  body={err}" if st >= 400 else "  OK"))
