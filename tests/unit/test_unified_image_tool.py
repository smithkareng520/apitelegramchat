"""统一图像工具 generate_image（原 generate_image_from_text / edit_image_with_reference 合并）回归测试。

覆盖四个层面：
1. Schema：单一工具进入 SEARCH_TOOLS，旧名退出；model enum 与
   "什么模型可编辑/仅能生成"的能力说明随配置自动推导。
2. 分发：image_url 缺省 -> 文生图；提供 -> 编辑；两个旧名别名按各自
   历史语义路由（generate_image_from_text 强制丢弃 image_url）。
3. 能力硬校验：仅支持文生图的模型携带 image_url 时立即返回可操作错误。
4. UI：工具折叠块 / 工具组折叠块（进行态与完成态）按生成/编辑分别显示；
   结果卡片标题同理。
"""
import asyncio

import pytest

from search.tool_schemas import (
    DUAL_MODE_MODELS,
    EDIT_MODELS,
    GENERATE_ONLY_MODELS,
    SEARCH_TOOLS,
    TEXT_ONLY_MODELS,
)


def _tool_defs() -> dict:
    return {
        t["function"]["name"]: t
        for t in SEARCH_TOOLS
        if isinstance(t, dict) and "function" in t
    }


# ---------------------------------------------------------------------------
# 1. Schema 合并
# ---------------------------------------------------------------------------

def test_unified_image_tool_replaces_legacy_pair():
    names = _tool_defs()
    assert "generate_image" in names
    # 旧名不再进入工具清单（dispatch 层保留隐藏别名，见别名测试）
    assert "generate_image_from_text" not in names
    assert "edit_image_with_reference" not in names


def test_unified_image_tool_schema_shape():
    tool = _tool_defs()["generate_image"]["function"]
    params = tool["parameters"]
    # image_url 可选：required 只含 prompt / model
    assert params["required"] == ["prompt", "model"]
    assert "image_url" in params["properties"]
    # model enum 覆盖全部图像模型（含仅文生图模型，enum 无法按模式拆分，
    # 能力边界靠描述说明 + 执行层硬校验兜底）
    assert params["properties"]["model"]["enum"] == TEXT_ONLY_MODELS


def test_unified_image_tool_description_lists_capabilities():
    desc = _tool_defs()["generate_image"]["function"]["description"]
    # 双模式语义
    assert "EDIT" in desc and "CREATE" in desc
    # 什么模型可编辑 / 什么只能生成，随配置自动列全
    for model in EDIT_MODELS:
        assert model in desc
    for model in GENERATE_ONLY_MODELS:
        assert model in desc


def test_unified_image_tool_description_states_dual_mode_semantics():
    """双能力模型（如 agnes-image-2.5-flash）同一端点 image_url 可选：

    描述必须把「操作由每次调用是否携带 image_url 决定（不是模型属性）」
    与「模型只约束 image_url 允不允许携带」讲清楚，而不是把模型呈现成
    非纯生成即纯编辑的两个互斥桶。
    """
    desc = _tool_defs()["generate_image"]["function"]["description"]
    # 操作按次调用决定，不由模型决定
    assert "PER CALL" in desc and "NOT by the model" in desc
    # 双能力桶语义：image_url 可选——省略即生成、提供即编辑
    assert "Dual-mode models" in desc and "OPTIONAL" in desc
    # 仅生成桶语义：不接受 image_url
    assert "NOT accepted" in desc
    # 结论句：任意模型都能生成，编辑需双能力模型
    assert "Any model can CREATE" in desc
    assert "EDIT requires a dual-mode model" in desc
    # 每个双能力模型都必须列在 Dual-mode 段内（而非仅出现在描述任意位置）
    dual_section = desc.split("Dual-mode models")[1].split("Generate-only models")[0]
    for model in DUAL_MODE_MODELS:
        assert model in dual_section


def test_dual_mode_models_is_edit_models_alias():
    # DUAL_MODE_MODELS 是 EDIT_MODELS 的显式别名（同一能力集合，
    # 仅显示口径不同：这些模型并非"只能编辑"，省略 image_url 同样能生成）
    assert DUAL_MODE_MODELS == EDIT_MODELS


def test_dual_mode_example_shows_same_model_both_operations():
    # 示例对必须演示"同一模型省略/携带 image_url 切换生成/编辑"：
    # 两例使用同一个双能力模型，第一例无 image_url，第二例有
    tool = _tool_defs()["generate_image"]["function"]
    examples = tool["input_examples"]
    assert len(examples) >= 2
    if DUAL_MODE_MODELS:
        assert examples[0]["model"] == examples[1]["model"]
        assert "image_url" not in examples[0]
        assert examples[1].get("image_url")


# ---------------------------------------------------------------------------
# 2. 分发路由（含旧名别名）
# ---------------------------------------------------------------------------

class _Captured:
    def __init__(self):
        self.kwargs = None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return "✅ ok"


def _dispatch(name: str, arguments: dict):
    import tool_dispatch

    captured = _Captured()
    original = tool_dispatch.execute_generate_image
    tool_dispatch.execute_generate_image = captured
    try:
        result = asyncio.run(
            tool_dispatch.dispatch_tool_call(name, arguments, 123)
        )
    finally:
        tool_dispatch.execute_generate_image = original
    assert result == "✅ ok"
    return captured.kwargs


def test_dispatch_generate_image_without_url_is_text_to_image():
    kwargs = _dispatch("generate_image", {"prompt": "一只猫", "model": "gpt-image-2"})
    assert kwargs["image_url"] is None
    assert kwargs["prompt"] == "一只猫"


def test_dispatch_generate_image_with_url_is_edit():
    kwargs = _dispatch("generate_image", {
        "prompt": "移除行人",
        "model": "gpt-image-2",
        "image_url": "https://example.com/ref.png",
    })
    assert kwargs["image_url"] == "https://example.com/ref.png"


def test_dispatch_legacy_generate_alias_forces_no_reference():
    # 旧名 generate_image_from_text 历史语义：强制文生图，
    # 即使误带 image_url 也必须丢弃。
    kwargs = _dispatch("generate_image_from_text", {
        "prompt": "一只猫",
        "model": "gpt-image-2",
        "image_url": "https://example.com/should-be-dropped.png",
    })
    assert kwargs["image_url"] is None


def test_dispatch_legacy_edit_alias_passes_reference():
    kwargs = _dispatch("edit_image_with_reference", {
        "prompt": "改成水彩画",
        "model": "gpt-image-2",
        "image_url": "https://example.com/ref.png",
    })
    assert kwargs["image_url"] == "https://example.com/ref.png"


# ---------------------------------------------------------------------------
# 3. 能力硬校验：仅文生图模型 + image_url -> 立即可操作错误
# ---------------------------------------------------------------------------

def _run_execute_generate_image(monkeypatch, *, model_info, image_url):
    import search.media_tools as mt

    monkeypatch.setattr(mt, "SUPPORTED_MODELS", {"text-only-model": model_info})
    return asyncio.run(mt.execute_generate_image(
        prompt="test", model="text-only-model", image_url=image_url,
    ))


def test_generate_only_model_rejects_image_url(monkeypatch):
    from types import SimpleNamespace

    model_info = SimpleNamespace(image_input=False, provider="openrouter")
    result = _run_execute_generate_image(monkeypatch, model_info=model_info, image_url="https://x/y.png")
    assert "仅支持文生图" in result
    # 可操作：告知应改用哪个模型
    for m in EDIT_MODELS:
        assert m in result


def test_generate_only_model_without_image_url_still_works(monkeypatch):
    """同一模型不带参考图时不应被能力校验拦截。"""
    from types import SimpleNamespace

    import search.media_tools as mt

    model_info = SimpleNamespace(image_input=False, provider="openrouter")

    captured = {}

    async def fake_dispatch(task):
        captured["task"] = task

        class _R:
            images = [b"fake-bytes"]
            text = ""
            refusal = ""
            endpoint = "/x"

        return _R()

    async def fake_upload(images):
        return ["https://example.com/generated.png"]

    monkeypatch.setattr(mt, "SUPPORTED_MODELS", {"text-only-model": model_info})
    monkeypatch.setattr(mt, "dispatch_image_task", fake_dispatch)
    monkeypatch.setattr(mt, "_upload_generated_images_to_r2", fake_upload)
    result = asyncio.run(mt.execute_generate_image(
        prompt="test", model="text-only-model", image_url=None,
    ))
    assert "已生成" in result
    from core.images import ImageTask
    assert captured["task"].operation == "generate"


# ---------------------------------------------------------------------------
# 4. UI 折叠块：生成 / 编辑 分别显示
# ---------------------------------------------------------------------------

def test_initial_summary_follows_image_url():
    from ai.tool_summary import _generate_initial_tool_summary

    assert _generate_initial_tool_summary("generate_image", {}) == "Generating an image"
    assert _generate_initial_tool_summary(
        "generate_image", {"image_url": "https://x/y.png"}) == "Editing an image"
    # 空串 / 空白 image_url 视为未携带
    assert _generate_initial_tool_summary("generate_image", {"image_url": ""}) == "Generating an image"
    # 多张仅限文生图
    assert _generate_initial_tool_summary("generate_image", {"num_images": 3}) == "Generating 3 images"


def test_done_summary_follows_image_url():
    from ai.tool_summary import _generate_tool_summary_done

    assert _generate_tool_summary_done("generate_image", {}, "r") == "Generated an image"
    assert _generate_tool_summary_done(
        "generate_image", {"image_url": "https://x/y.png"}, "r") == "Edited an image"


def test_group_summary_distinguishes_generate_and_edit():
    from ai.rich_message_builder import RichMessageBuilder

    b = RichMessageBuilder(chat_id=1)
    group = {"items": [
        {"id": "1", "type": "generate_image", "status": "done", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Generated an image"

    group = {"items": [
        {"id": "1", "type": "generate_image", "status": "done",
         "fn_args": {"image_url": "https://x/y.png"}},
    ]}
    assert b._generate_group_summary(group) == "Edited an image"

    # 混合批次：首字母大写规范 + 分别聚合
    group = {"items": [
        {"id": "1", "type": "generate_image", "status": "done", "fn_args": {}},
        {"id": "2", "type": "generate_image", "status": "done",
         "fn_args": {"image_url": "https://x/y.png"}},
    ]}
    assert b._generate_group_summary(group) == "Generated an image, edited an image"


def test_group_summary_legacy_names_aggregate():
    from ai.rich_message_builder import RichMessageBuilder

    b = RichMessageBuilder(chat_id=1)
    group = {"items": [
        {"id": "1", "type": "generate_image_from_text", "status": "done", "fn_args": {}},
        {"id": "2", "type": "edit_image_with_reference", "status": "done",
         "fn_args": {"image_url": "https://x/y.png"}},
    ]}
    assert b._generate_group_summary(group) == "Generated an image, edited an image"


def test_outer_summary_follows_image_url_while_running():
    from ai.rich_message_builder import RichMessageBuilder

    b = RichMessageBuilder(chat_id=1)
    # 测试环境无事件循环，屏蔽 flush 定时器（生产中在 async 上下文运行）
    b.request_flush = lambda force=False: None
    b.start_new_tool_group()
    group = b._tool_groups[-1]

    b.add_tool_item("t1", "generate_image", "...")
    b.update_tool_args("t1", {})
    b._refresh_outer_summary(group)
    assert group["outer_summary"] == "Generating an image"

    b.add_tool_item("t2", "generate_image", "...")
    b.update_tool_args("t2", {"image_url": "https://x/y.png"})
    b._refresh_outer_summary(group)
    assert group["outer_summary"] == "Editing an image"


def test_result_card_follows_image_url():
    from tool_result_format import format_tool_result

    # 与 execute_generate_image._format_success_links 的真实返回格式一致：
    # "图片链接：" 单独一行，URL 每行一个。
    ok = "✅ 已生成 1 张图片。\n图片链接：\nhttps://example.com/a.png"
    summary, details = asyncio.run(format_tool_result(
        "generate_image", {"prompt": "p", "model": "gpt-image-2"}, ok))
    assert summary == "🎨 Generated 1 image"
    assert "已生成 1 张图片" in details

    summary, details = asyncio.run(format_tool_result(
        "generate_image",
        {"prompt": "p", "model": "gpt-image-2", "image_url": "https://x/in.png"},
        ok))
    assert summary == "🎨 Edited 1 image"
    assert "已编辑 1 张图片" in details

    summary, _ = asyncio.run(format_tool_result(
        "generate_image",
        {"prompt": "p", "model": "gpt-image-2", "image_url": "https://x/in.png"},
        "❌ HTTP 状态：404"))
    assert summary == "🎨 图片编辑失败"
