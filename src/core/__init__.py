"""core 包：utils.py 拆分后的底层子系统。

依赖方向（单向，无环）：
  logging_setup <- http_session / chat_guard / text_utils
  text_utils    <- rich_media
  rich_media    <- telegram_messaging
  (balances / message_extract 仅依赖 logging_setup)

utils.py 保留为兼容 facade；新代码请直接从 core.* 子模块导入。
"""
