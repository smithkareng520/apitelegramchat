"""search 包：工具子系统（web_search / fetch_url / 生活查询 / 媒体生成 /
text_editor）。

原 search_engine.py 单体按职责拆分后的形态，兼容 facade 已随直接导入的
收敛而移除。依赖方向单向：tool_schemas/model_catalog 为数据底座，
caches 独立，serper/fetch_url/quick_lookup/media_tools/text_editor
面向 tool_executors 与 mcpserver 暴露 execute_* 入口。
（MCP 化重构后 map_tools.py 已删除：地图能力直接经 gaode_mcp 暴露。）
"""
