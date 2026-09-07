"""search 包：search_engine.py 拆分后的工具子系统。

原 3997 行单体按职责拆为 8 个子模块；search_engine.py 保留为兼容
facade。依赖方向单向：tool_schemas/model_catalog 为数据底座，
caches 独立，serper/fetch_url/quick_lookup/map_tools/media_tools
面向 tool_executors 与 mcpserver 暴露 execute_* 入口。
"""
