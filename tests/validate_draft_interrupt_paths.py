#!/usr/bin/env python3
"""草稿中断相关输入路径的独立回归测试。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.app as app_module  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def test_media_group_wait_is_managed_and_cancelable() -> None:
    """首个媒体组分片创建的聚合等待必须属于 active_tasks，并能被下一输入取消。"""
    chat_id = 8801
    group_id = "photo-group-1"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_process(_chat_id: int, _group_id: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    original_process = app_module._process_media_group_once
    app_module._process_media_group_once = fake_process
    app_module._media_group_tasks.clear()
    async with app_module.active_tasks_lock:
        app_module.active_tasks.pop(chat_id, None)
    try:
        await app_module._schedule_media_group(chat_id, group_id)
        task = app_module._media_group_tasks[group_id]
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # 同一媒体组的后续分片不应重建或取消正在等待的任务。
        await app_module._schedule_media_group(chat_id, group_id)
        require(app_module._media_group_tasks[group_id] is task, "同一媒体组必须复用聚合任务")
        async with app_module.active_tasks_lock:
            require(app_module.active_tasks.get(chat_id) is task, "聚合等待任务必须登记为当前可取消任务")

        await app_module._cancel_old_task(chat_id)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        require(task.cancelled(), "新输入取消时，媒体组聚合等待任务必须立即结束")
    finally:
        app_module._process_media_group_once = original_process
        app_module._media_group_tasks.clear()
        async with app_module.active_tasks_lock:
            app_module.active_tasks.pop(chat_id, None)


async def test_document_group_wait_is_managed_and_cancelable() -> None:
    """文档组的聚合等待也必须遵循与图片组相同的中断契约。"""
    chat_id = 8802
    group_id = "document-group-1"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_process(_chat_id: int, _group_id: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    original_process = app_module._process_document_group_once
    app_module._process_document_group_once = fake_process
    app_module._document_group_tasks.clear()
    async with app_module.active_tasks_lock:
        app_module.active_tasks.pop(chat_id, None)
    try:
        await app_module._schedule_document_group(chat_id, group_id)
        task = app_module._document_group_tasks[group_id]
        await asyncio.wait_for(started.wait(), timeout=1.0)
        async with app_module.active_tasks_lock:
            require(app_module.active_tasks.get(chat_id) is task, "文档组等待任务必须登记为当前可取消任务")

        await app_module._cancel_old_task(chat_id)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        require(task.cancelled(), "新输入取消时，文档组聚合等待任务必须立即结束")
    finally:
        app_module._process_document_group_once = original_process
        app_module._document_group_tasks.clear()
        async with app_module.active_tasks_lock:
            app_module.active_tasks.pop(chat_id, None)


def main() -> None:
    asyncio.run(test_media_group_wait_is_managed_and_cancelable())
    asyncio.run(test_document_group_wait_is_managed_and_cancelable())
    print("draft interrupt path validation: PASS")


if __name__ == "__main__":
    main()
