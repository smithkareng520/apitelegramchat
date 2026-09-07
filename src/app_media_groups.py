"""媒体组/视频组/文档组聚合处理（自 app.py 拆出）。

相册分片先入 state 媒体组存储，聚合等待 MEDIA_GROUP_TIMEOUT 秒后
合并为一条 user 消息触发一轮 AI 回合；混合相册（photo+video）按
后缀分流，互不争抢。
"""
import asyncio
import os
import mimetypes

MEDIA_GROUP_TIMEOUT = 5
from typing import cast

from config import SUPPORTED_MODELS
from state import (
    user_contexts,
    user_models,
    get_or_init_context,
    get_user_model,
    get_chat_lock,
    pop_media_group,
    set_current_user_namespace,
)
from utils import send_rich_html_message, get_logger
from ai_handlers import get_ai_response
from file_handlers import download_file
from workspace_paths import workspace_download_root
from workspace_utils import init_workspace
from app_turns import (
    pre_flight_context_check,
    update_conversation_and_ledger,
    active_tasks,
    active_tasks_lock,
    _cleanup_task,
    _handle_text_message,
    get_user_info,
    is_authorized,
    reply_unauthorized,
    _get_reply_context,
)


logger = get_logger(__name__)


_media_group_tasks: dict[str, asyncio.Task] = {}
_document_group_tasks: dict[str, asyncio.Task] = {}
# 视频组任务表：key 为 f"{media_group_id}:video"（与图片组 :photo 后缀分流，
# 避免混合相册里两类分片互相争抢聚合存储 / 互相取消任务）。
_video_group_tasks: dict[str, asyncio.Task] = {}
async def _process_media_group_once(chat_id: int, media_group_id: str) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
        messages = await pop_media_group(media_group_id)
        _media_group_tasks.pop(media_group_id, None)
        if not messages:
            return

        first_msg = messages[0]
        username, user_id = get_user_info(first_msg)
        set_current_user_namespace(user_id or str(chat_id))
        if not is_authorized(username, user_id):
            await reply_unauthorized(chat_id, first_msg.get("message_id"))
            return

        file_ids = []
        captions = []
        for msg in messages:
            if "photo" in msg:
                file_ids.append(msg["photo"][-1]["file_id"])
            if msg.get("caption"):
                captions.append(msg["caption"].strip())
        if not file_ids:
            return

        combined_caption = " ".join(c for c in captions if c)
        context_prefix = _get_reply_context(first_msg)
        if context_prefix:
            combined_caption = context_prefix + combined_caption

        content_text = f"📎 用户上传了图片组（共 {len(file_ids)} 张）"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请描述这组图片的内容"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": file_ids,
            "type": "photo_group",
            "attachments": [
                {
                    "kind": "photo",
                    "file_id": fid,
                }
                for fid in file_ids
            ],
        }

        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的图片组附加内容过长，已超过模型单次处理极限，请精简发送。")
            return

        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_process_media_group_once 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理图片组时出错</b>\n<code>{str(e)[:100]}</code>")
async def _process_video_group_once(chat_id: int, group_key: str) -> None:
    """聚合处理视频相册（对称 _process_media_group_once）。

    group_key 为 f"{media_group_id}:video"。等待聚合期结束后弹出全部分片，
    组装 type="video_group" 的 user_message（file_ids / file_names /
    mime_types 数组），由 _resolve_multimodal_content 按当前模型能力解析：
    支持视频的模型收到多个 video_url content part，不支持的模型收到文本
    占位——与单视频、图片组行为完全一致，切换模型不丢信息。
    """
    try:
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
        messages = await pop_media_group(group_key)
        _video_group_tasks.pop(group_key, None)
        if not messages:
            return

        first_msg = messages[0]
        username, user_id = get_user_info(first_msg)
        set_current_user_namespace(user_id or str(chat_id))
        if not is_authorized(username, user_id):
            await reply_unauthorized(chat_id, first_msg.get("message_id"))
            return

        video_items = []
        captions = []
        for gmsg in messages:
            media = gmsg.get("video") or gmsg.get("video_note")
            if media and media.get("file_id"):
                fid = media["file_id"]
                video_items.append({
                    "file_id": fid,
                    "file_name": media.get("file_name") or f"video_{fid[:8]}.mp4",
                    "mime_type": media.get("mime_type") or "video/mp4",
                })
            if gmsg.get("caption"):
                captions.append(gmsg["caption"].strip())
        if not video_items:
            return

        combined_caption = " ".join(c for c in captions if c)
        context_prefix = _get_reply_context(first_msg)
        if context_prefix:
            combined_caption = context_prefix + combined_caption

        content_text = f"📎 用户上传了视频组（共 {len(video_items)} 个）"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请分析这组视频的内容"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": [v["file_id"] for v in video_items],
            "file_names": [v["file_name"] for v in video_items],
            "mime_types": [v["mime_type"] for v in video_items],
            "type": "video_group",
            "attachments": [
                {
                    "kind": "video",
                    "file_id": v["file_id"],
                    "file_name": v["file_name"],
                    "mime_type": v["mime_type"],
                }
                for v in video_items
            ],
        }

        is_safe = await pre_flight_context_check(chat_id, user_message)
        if not is_safe:
            await send_rich_html_message(chat_id, "⚠️ <b>发送失败</b><br/>您当前发送的视频组附加内容过长，已超过模型单次处理极限，请精简发送。")
            return

        full, _, new_msgs, usage = await get_ai_response(
            chat_id, user_models, user_contexts, username,
            user_message=user_message,
        )
        if full and not full.startswith(("⚠️", "❌")):
            await update_conversation_and_ledger(chat_id, user_message, new_msgs, usage)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"_process_video_group_once 异常: {e}")
        await send_rich_html_message(chat_id, f"❌ <b>处理视频组时出错</b>\n<code>{str(e)[:100]}</code>")
async def _process_document_group_once(chat_id: int, media_group_id: str) -> None:
    # 与图片/视频组对称的异常保护：下载/建目录等任一环节抛异常时，
    # 用户能收到错误提示，而不是任务静默终止、只留下
    # "Task exception was never retrieved" 日志。
    try:
        await _process_document_group_inner(chat_id, media_group_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"处理文档组异常 group={media_group_id}: {e}")
        try:
            await send_rich_html_message(
                chat_id, "❌ <b>处理文档组时出错</b>\n<code>" + str(e)[:100] + "</code>"
            )
        except Exception:
            logger.debug("_process_document_group_once 内部忽略的异常", exc_info=True)
            pass


async def _process_document_group_inner(chat_id: int, media_group_id: str) -> None:
    asyncio.create_task(init_workspace(chat_id))
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    messages = await pop_media_group(media_group_id)
    _document_group_tasks.pop(media_group_id, None)
    if not messages:
        return

    first_msg = messages[0]
    username, user_id = get_user_info(first_msg)
    set_current_user_namespace(user_id or str(chat_id))
    if not is_authorized(username, user_id):
        await reply_unauthorized(chat_id, first_msg.get("message_id"))
        return

    file_ids = []
    file_names = []
    mime_types = []
    captions = []
    for msg in messages:
        if "document" in msg:
            doc = msg["document"]
            file_ids.append(doc["file_id"])
            fname = doc.get("file_name") or f"document_{doc['file_id'][:8]}.bin"
            file_names.append(fname)
            mime_types.append(doc.get("mime_type", mimetypes.guess_type(fname)[0] or "application/pdf"))
            if msg.get("caption"):
                captions.append(msg["caption"].strip())

    combined_caption = " ".join(c for c in captions if c) if captions else ""

    lock = await get_chat_lock(chat_id)
    async with lock:
        current_model = get_user_model(chat_id)
        model_info = SUPPORTED_MODELS.get(current_model)
        supports_native_document = bool(model_info.native_document) if model_info else False

    if supports_native_document:
        content_text = f"📎 用户上传了文档组（共 {len(file_ids)} 个文件）：{', '.join(file_names)}"
        if combined_caption:
            content_text += f"\n\n{combined_caption}"
        else:
            content_text += "\n\n请直接阅读并分析这些文档。"

        user_message = {
            "role": "user",
            "content": content_text,
            "file_ids": file_ids,
            "file_names": file_names,
            "mime_types": mime_types,
            "type": "document_group",
            "attachments": [
                {
                    "kind": "document",
                    "file_id": fid,
                    "file_name": fname,
                    "mime_type": mime,
                }
                for fid, fname, mime in zip(file_ids, file_names, mime_types)
            ],
        }
    else:
        workspace = workspace_download_root(chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        downloaded = []
        failed = []
        for fid, fname in zip(file_ids, file_names):
            safe_fname = os.path.basename(fname)
            target_path = workspace / safe_fname
            counter = 1
            while target_path.exists():
                name, ext = os.path.splitext(safe_fname)
                target_path = workspace / f"{name}_{counter}{ext}"
                counter += 1
            # download_file 内部已经把字节缓存到 R2 的 telegram/{file_id} 前缀，
            # download/ 只是本地落地缓冲，不需要再往 R2 镜像一份。
            success = await download_file(fid, str(target_path))
            if success:
                downloaded.append(target_path.name)
            else:
                failed.append(safe_fname)

        if not downloaded:
            content_text = "📎 用户上传了文档组，但所有文件下载失败，请稍后重试。"
        else:
            file_list = "、".join(downloaded)
            content_text = (
                f"📎 用户上传了文档组（共 {len(downloaded)} 个文件）：{file_list}，"
                f"已保存在工作区根目录的 download/ 子目录，可直接访问。"
            )
            if failed:
                content_text += f"\n⚠️ 以下文件下载失败：{', '.join(failed)}，请重新发送。"
            if combined_caption:
                content_text += f"\n\n用户指令：{combined_caption}"
            else:
                content_text += (
                    "\n\n请根据用户指令处理这些文档。可用 bash 执行 `ls -la download/` 查看可用文件，"
                    "再直接读取（如 `cat download/<文件名>`），或用 text_editor（path 填 download/<文件名>）查看。"
                )

        user_message = {"role": "user", "content": content_text, "file_ids": file_ids, "file_names": file_names, "mime_types": mime_types, "type": "document_group", "attachments": [{"kind": "document", "file_id": fid, "file_name": fname, "mime_type": mime} for fid, fname, mime in zip(file_ids, file_names, mime_types)]}

    lock = await get_chat_lock(chat_id)
    async with lock:
        ctx = get_or_init_context(chat_id)
        ctx["username"] = username or f"User_{chat_id}"
        ctx["tg_username"] = (username or "").strip()
    # 两个分支的 content 均由 str 类型的 content_text 构造，cast 仅用于
    # 消除 dict 联合推断带来的宽化，运行时值恒为 str。
    await _handle_text_message(chat_id, cast(str, user_message.get("content", "")), username, user_message)

async def _schedule_group(chat_id: int, group_key: str, tasks: dict, coro_factory) -> None:
    """统一相册聚合调度（合并原图片/视频/文档三份相同样板）。

    去重（同组已排队直接返回）→ create_task → 登记为当前 chat 的可取消
    生成任务 → 完成回调清理组表并通知 _cleanup_task。
    """
    if group_key in tasks:
        return
    task = asyncio.create_task(coro_factory(chat_id, group_key))
    tasks[group_key] = task
    async with active_tasks_lock:
        active_tasks[chat_id] = task

    def _done(done_task: asyncio.Task) -> None:
        if tasks.get(group_key) is done_task:
            tasks.pop(group_key, None)
        asyncio.create_task(_cleanup_task(chat_id, done_task))

    task.add_done_callback(_done)


async def _schedule_media_group(chat_id: int, media_group_id: str) -> None:
    """将图片组的等待/处理任务作为当前 chat 的可取消生成任务登记。"""
    await _schedule_group(chat_id, media_group_id, _media_group_tasks, _process_media_group_once)


async def _schedule_video_group(chat_id: int, group_key: str) -> None:
    """将视频组的等待/处理任务作为当前 chat 的可取消生成任务登记。"""
    await _schedule_group(chat_id, group_key, _video_group_tasks, _process_video_group_once)


async def _schedule_document_group(chat_id: int, media_group_id: str) -> None:
    """将文档组的等待/处理任务作为当前 chat 的可取消生成任务登记。"""
    await _schedule_group(chat_id, media_group_id, _document_group_tasks, _process_document_group_once)

