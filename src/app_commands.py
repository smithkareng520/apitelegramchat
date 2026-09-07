"""命令与回调处理（自 app.py process_update 抽出）。

包含：管理员命令（/adduser /deluser /listusers /webhookinfo）、
/start 欢迎语、用户文本命令（/role /balance /model /clear /show）、
inline 按钮回调查询。返回 bool 的 *_command 供 process_update
按「是否已处理」短路分发。
"""
import html
import time
from typing import Any

from config import (
    BASE_URL,
    SUPPORTED_MODELS,
    SUPPORTED_ROLES,
    INGEST_MODE,
    add_whitelist_user,
    remove_whitelist_user,
    snapshot_whitelist,
    ADD_ADDED,
    ADD_ADMIN_REJECTED,
    ADD_SYNC_FAILED,
    REMOVE_REMOVED,
    REMOVE_ADMIN_REJECTED,
    REMOVE_SYNC_FAILED,
)
from state import (
    role_message_ids,
    get_user_role,
    set_user_role,
    get_user_model,
    safe_clear_history,
    safe_set_user_model,
    safe_clear_active_skill,
    get_chat_lock,
    get_or_init_context,
    get_show_drafts,
    set_show_drafts,
)
from utils import send_rich_html_message, query_provider_balances, get_logger
from message_user_tool import resolve_callback as resolve_message_user_callback
from webhook_sync import get_webhook_info, mask_webhook_url
from core.http_session import get_http_session
import proactive
import app_state
from app_state import update_queue, WEBHOOK_QUEUE_MAXSIZE
from app_turns import _cmd_match, _reply_params, is_admin, is_authorized, _interrupt_active_generation
from app_lists import (
    _send_via_send_message,
    update_role_list,
    update_model_list,
    send_role_list,
    send_model_list,
)


logger = get_logger(__name__)


async def _answer_callback_query(callback_query_id: str, text: str, show_alert: bool = False) -> None:
    """应答回调查询（合并原 6 处裸建 ClientSession，复用全局 HTTP 会话）。"""
    try:
        session = await get_http_session()
        await session.post(
            f"{BASE_URL}/answerCallbackQuery",
            json={
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )
    except Exception as e:
        logger.debug(f"answerCallbackQuery 失败(可忽略): {e}")


_ADMIN_COMMANDS = ("/adduser", "/deluser", "/listusers", "/webhookinfo")


async def _handle_admin_commands(chat_id: int, msg: dict, text: str, username: str, user_id: str) -> bool:
    """管理员命令分发。返回 True 表示 update 已处理（含权限拒绝）。"""
    # 门控：只有消息确实是管理员命令时才进入权限校验/分发；普通聊天
    # 与其他命令直接放行（返回 False）。否则非管理员用户的每一条消息
    # 都会被误回复"权限不足"，白名单用户将永远无法对话。
    if not any(_cmd_match(text, cmd) for cmd in _ADMIN_COMMANDS):
        return False
    if not is_admin(username, user_id):
        await send_rich_html_message(chat_id, "❌ <b>权限不足</b>\n只有管理员可以执行此操作。", reply_parameters=_reply_params(msg["message_id"]))
        return True
    # 不用进程级锁包裹这三个命令：那会让白名单操作和其他用户
    # 正在做的 /model /role 等命令排队等待，admin 命令之间也会
    # 互相等待。白名单的读改写由 config.add_whitelist_user /
    # remove_whitelist_user / snapshot_whitelist 内部的
    # _whitelist_lock 原子完成（改内存集合 + 落盘在同一把锁内，
    # 不会有裸读写竞态）。这把锁只保护白名单文件本身，不会阻塞
    # 其他用户或其他命令。
    if _cmd_match(text, "/adduser"):
        parts = text.split()
        if len(parts) != 2:
            await send_rich_html_message(chat_id, "❌ <b>用法错误</b>\n用法：<code>/adduser @username</code> 或 <code>/adduser 123456789</code>", reply_parameters=_reply_params(msg["message_id"]))
            return True
        target = parts[1].strip().lstrip("@")
        if not target:
            await send_rich_html_message(chat_id, "❌ <b>输入无效</b>\n请输入有效的用户名或ID。", reply_parameters=_reply_params(msg["message_id"]))
            return True
        # result 在本函数多个分支被复用（/adduser、/deluser 的
        # str 状态码与 /balance 的 BalanceResult 遍历），形状异构，
        # 首次绑定处标注 Any 以如实反映用法。
        result: Any = await add_whitelist_user(target)
        target_html = html.escape(target)
        if result == ADD_ADDED:
            reply = f"✅ <b>添加成功</b>\n已添加 <code>{target_html}</code> 到白名单，并已同步到 R2。"
        elif result == ADD_SYNC_FAILED:
            reply = (f"✅ <b>已添加（本地）</b>\n<code>{target_html}</code> 已加入白名单并写入本地文件。\n"
                     f"⚠️ 推送到 R2 失败：重启后该添加可能丢失，请检查 R2 配置后重试，或重新执行 /adduser。")
        elif result == ADD_ADMIN_REJECTED:
            reply = (f"❌ <b>拒绝操作</b>\n<code>{target_html}</code> 是管理员，不能加入用户白名单。\n"
                     f"管理员始终拥有全部权限，无需加入白名单（这也是用户白名单与管理员名单的区别）。")
        else:  # ADD_EXISTS
            reply = f"ℹ️ <b>无需操作</b>\n<code>{target_html}</code> 已在白名单中。"
        await send_rich_html_message(chat_id, reply, reply_parameters=_reply_params(msg["message_id"]))
        return True
    elif _cmd_match(text, "/deluser"):
        parts = text.split()
        if len(parts) != 2:
            await send_rich_html_message(chat_id, "❌ <b>用法错误</b>\n用法：<code>/deluser @username</code> 或 <code>/deluser 123456789</code>", reply_parameters=_reply_params(msg["message_id"]))
            return True
        target = parts[1].strip().lstrip("@")
        if not target:
            await send_rich_html_message(chat_id, "❌ <b>输入无效</b>\n请输入有效的用户名或ID。", reply_parameters=_reply_params(msg["message_id"]))
            return True
        result = await remove_whitelist_user(target)
        target_html = html.escape(target)
        if result == REMOVE_REMOVED:
            reply = f"✅ <b>移除成功</b>\n已移除 <code>{target_html}</code>，并已同步到 R2。"
        elif result == REMOVE_SYNC_FAILED:
            reply = (f"✅ <b>已移除（本地）</b>\n<code>{target_html}</code> 已从白名单移除并写入本地文件。\n"
                     f"⚠️ 推送到 R2 失败：重启后该用户可能恢复访问，请检查 R2 配置后重试，或重新执行 /deluser。")
        elif result == REMOVE_ADMIN_REJECTED:
            reply = (f"❌ <b>拒绝操作</b>\n<code>{target_html}</code> 是管理员，不能从用户白名单删除。\n"
                     f"管理员不属于用户白名单，其权限不通过 /deluser 管理。")
        else:  # REMOVE_MISSING
            reply = f"❌ <b>用户不存在</b>\n<code>{target_html}</code> 不在白名单中。"
        await send_rich_html_message(chat_id, reply, reply_parameters=_reply_params(msg["message_id"]))
        return True
    elif _cmd_match(text, "/listusers"):
        users = await snapshot_whitelist()
        if not users:
            await send_rich_html_message(chat_id, "📋 <b>白名单为空</b>", reply_parameters=_reply_params(msg["message_id"]))
        else:
            users_list = "".join(f"<li><code>{str(u)}</code></li>" for u in users)
            await send_rich_html_message(chat_id, f"📋 <b>当前白名单用户：</b>\n<ul>{users_list}</ul>", reply_parameters=_reply_params(msg["message_id"]))
        return True
    elif _cmd_match(text, "/webhookinfo"):
        # 观测命令：把 getWebhookInfo 的关键投递链路指标带回聊天，
        # 让"积压"（pending_update_count）与最近投递错误无需登服务器
        # 看日志即可发现。URL 先脱敏，token 不进聊天记录。
        info = await get_webhook_info()
        if not info:
            await send_rich_html_message(chat_id, "❌ <b>获取失败</b>\ngetWebhookInfo 请求失败，请查看服务端日志。", reply_parameters=_reply_params(msg["message_id"]))
            return True
        pending = info.get("pending_update_count", 0)
        if INGEST_MODE == "polling":
            # 轮询模式下 url 应为空；若非空说明 deleteWebhook 没生效，
            # getUpdates 会持续 409，必须立刻暴露出来。
            registered = info.get("url") or ""
            lines = [
                "🩺 <b>投递链路状态（getUpdates 长轮询）</b>",
                "<blockquote>",
                "摄取模式：<code>polling</code>（不经过边缘 WAF，含 shell 特征的消息可正常送达）",
                f"轮询任务：<b>{'运行中 ✅' if (app_state._telegram_polling_task and not app_state._telegram_polling_task.done()) else '未运行 ❌'}</b>",
                f"队列水位：<code>{update_queue.qsize()}/{WEBHOOK_QUEUE_MAXSIZE}</code>",
                f"Telegram 侧积压：<b>{pending}</b>",
                f"webhook 注册：<code>{mask_webhook_url(registered) if registered else '已注销（正确）'}</code>",
                "</blockquote>",
            ]
            if registered:
                lines.append(
                    "⚠️ webhook 仍处于注册状态，getUpdates 会返回 409 Conflict。"
                    "重启服务会自动注销；或手动调用 <code>deleteWebhook</code>。"
                )
            else:
                lines.append("✅ 链路正常：消息经出站响应体拉取，不受 Cloudflare 入站请求体规则影响。")
            await send_rich_html_message(chat_id, "\n".join(lines), reply_parameters=_reply_params(msg["message_id"]))
            return True
        lines = [
            "🩺 <b>Webhook 投递链路状态</b>",
            "<blockquote>",
            f"注册 URL：<code>{mask_webhook_url(info.get('url') or '')}</code>",
            f"积压 update：<b>{pending}</b>",
            f"IP 地址：<code>{info.get('ip_address') or '—'}</code>",
            f"最大连接数：<code>{info.get('max_connections', '—')}</code>",
        ]
        last_err_date = info.get("last_error_date")
        last_err_msg = info.get("last_error_message")
        if last_err_msg:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_err_date)) if last_err_date else "未知时间"
            lines.append(f"最近投递错误：<code>{str(ts)} / {str(last_err_msg)[:200]}</code>")
        else:
            lines.append("最近投递错误：无 ✅")
        lines.append("</blockquote>")
        if pending:
            lines.append(
                "⚠️ 存在积压：应用健康运行并对重放返回 200 后，Telegram 会自动重放排干"
                "（bot 会迟到地回复这批消息）。若希望丢弃：设置 <code>DROP_PENDING_ON_STARTUP=true</code> "
                "后重启，或手动调用 <code>deleteWebhook?drop_pending_updates=true</code> 后重新 setWebhook。"
            )
        else:
            lines.append("✅ 无积压，投递链路正常。")
        await send_rich_html_message(chat_id, "\n".join(lines), reply_parameters=_reply_params(msg["message_id"]))
        return True
    return False


async def _handle_start_command(chat_id: int, msg: dict, text: str, username: str, user_id: str) -> bool:
    """处理 /start 欢迎语。返回 True 表示 update 已处理。"""
    # 门控：仅当消息确实是 /start（兼容 /start@botname）时才回复欢迎语。
    # 此函数位于 process_update 分发链最上游，若不检查文本内容，任何
    # 消息（包括普通聊天、媒体消息）都会被欢迎语拦截并 return True，
    # 导致消息永远到不了 AI 对话分支。
    if not _cmd_match(text, "/start"):
        return False
    authorized = is_authorized(username, user_id)
    if authorized:
        welcome_msg = """
<b>🤖 欢迎使用 AI 助手！</b></br>
✅ 您已获得授权，可以直接发送消息与我对话。</br>
<u>支持的功能：</u></br>
<blockquote>
<ul>
    <li><b>📝 文本对话</b> - 自然语言交流</li>
    <li><b>🖼️ 图片分析</b> - 图像识别与描述</li>
    <li><b>📄 文档解析</b> - PDF、Word等文件处理</li>
    <li><b>🔗 联网搜索</b> - 实时信息查询</li>
</ul>
</blockquote>
</br>💡 <i>提示：如需帮助，请联系管理员 @dearella</i>
"""
    else:
        welcome_msg = """
<b>🤖 欢迎使用 AI 助手！</b></br>
⚠️ <b>注意</b>：此机器人启用了白名单机制。</br>
请联系管理员 <b>@dearella</b></br> 申请白名单后，才能使用全部功能。
"""
    await send_rich_html_message(chat_id, welcome_msg, reply_parameters=_reply_params(msg["message_id"]))
    return True


async def _handle_text_command(chat_id: int, msg: dict, user_input: str) -> bool:
    """用户文本命令分发（/role /balance /model /clear /show）。返回 True 表示已处理。"""
    if _cmd_match(user_input, "/role"):
        cr = await get_user_role(chat_id)
        prev_mid = role_message_ids.get(chat_id)
        if not prev_mid or not await update_role_list(chat_id, prev_mid, SUPPORTED_ROLES, cr):
            mid = await send_role_list(chat_id, SUPPORTED_ROLES, cr, msg.get("message_id"))
            if mid:
                role_message_ids[chat_id] = mid
        return True

    if _cmd_match(user_input, "/balance"):
        parts = user_input.split(maxsplit=1)
        svc = parts[1].lower() if len(parts) > 1 else None
        if svc == "all":
            svc = None
        results = await query_provider_balances(svc)
        msgs = []
        for result in results:
            provider = result["provider"]
            if not result.get("ok"):
                msgs.append(
                    f"⚠️ <b>{provider}</b>: 查询失败 "
                    f"<i>({result.get('error', '未知错误')})</i>"
                )
                continue
            remaining = result.get("remaining")
            if result.get("unlimited"):
                value = "无限制"
            elif remaining is None:
                value = "未知"
            else:
                value = f"{remaining} {result.get('currency', '')}".strip()
            details = []
            if result.get("usage") is not None:
                details.append(f"已用 {result['usage']:.3f} USD")
            if result.get("available") is False:
                details.append("当前不可用")
            suffix = f"（{'；'.join(details)}）" if details else ""
            msgs.append(f"💰 <b>{provider}</b>: <code>{value}</code>{suffix}")
        if not msgs:
            msgs.append("⚠️ 暂无可查询的提供商余额")
        # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
        await _send_via_send_message(
            chat_id,
            "\n".join(msgs),
            reply_message_id=msg["message_id"],
        )
        return True

    if _cmd_match(user_input, "/model"):
        if (msg.get("chat") or {}).get("type") != "private":
            await _send_via_send_message(
                chat_id,
                "❌ <b>操作受限</b>\n模型切换仅限私聊使用。",
                reply_message_id=msg["message_id"],
            )
            return True
        # get_user_role 内部已用 state._role_lock 保护，无需再套
        # 进程级锁——那会让 /model 命令等待其他用户正在做的
        # /adduser 等操作释放锁。
        current_role = await get_user_role(chat_id)
        banner_html = ""
        if current_role:
            banner_html = f"⚠️ <b>兼容性提示</b>\n当前角色「<b>{current_role}</b>」可能与新模型不兼容，请留意。"
        await send_model_list(
            chat_id,
            list(SUPPORTED_MODELS.keys()),
            msg.get("message_id"),
            banner_html=banner_html,
        )
        return True

    if _cmd_match(user_input, "/clear"):
        await _interrupt_active_generation(chat_id)
        await safe_clear_history(chat_id)
        await safe_clear_active_skill(chat_id)
        lock = await get_chat_lock(chat_id)
        async with lock:
            ctx = get_or_init_context(chat_id)
            ctx["last_prompt_tokens"] = 0
            ctx["last_completion_tokens"] = 0
            ctx["token_ledger"] = []
        # /clear 是语义边界：历史清空意味着重新开始，主动唤醒
        # timer 也重置为随机 5~20min 下一次。
        try:
            await proactive.reset_proactive_timer(chat_id)
        except Exception as e:
            logger.warning(f"reset_proactive_timer 异常: {e}")
        await send_rich_html_message(chat_id, "✅ <b>操作成功</b>\n对话历史已清空", reply_parameters=_reply_params(msg["message_id"]))
        return True

    # ── /show on|off：草稿预览开关（USER 与 TIMER 回合统一生效）──
    if _cmd_match(user_input, "/show"):
        parts = user_input.split()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        if arg in ("on", "开", "1", "true"):
            await set_show_drafts(chat_id, True)
            await _send_via_send_message(
                chat_id,
                "✅ <b>草稿预览已开启</b>\n"
                "你发送消息和后台主动巡检（TIMER）时，都会实时展示"
                "富文本草稿，最终回复自动送达。",
                reply_message_id=msg["message_id"],
            )
        elif arg in ("off", "关", "0", "false"):
            await set_show_drafts(chat_id, False)
            await _send_via_send_message(
                chat_id,
                "✅ <b>草稿预览已关闭（静默模式）</b>\n"
                "过程不再实时展示。交付规则分两种：\n"
                "①你主动发消息时<b>默认交付</b>——最后一条回复正文（不含"
                "中间过程）会在回合结束时自动发送给你（AI 也可主动提前发送；"
                "仅当 AI 明确选择不发送时本轮才完全静默）；\n"
                "②后台主动巡检（TIMER）时默认静默——AI 认为需要你看到"
                "结论时才会主动发送（deliver_reply，不经草稿），不发送则"
                "本轮完全静默。提问/留言走 message_user。",
                reply_message_id=msg["message_id"],
            )
        else:
            current_show = await get_show_drafts(chat_id)
            state_line = "开启（/show on）" if current_show else "关闭（/show off）"
            await _send_via_send_message(
                chat_id,
                f"ℹ️ 当前草稿预览：<b>{state_line}</b>\n"
                "用法：<code>/show on</code> 开启 · <code>/show off</code> 关闭",
                reply_message_id=msg["message_id"],
            )
        return True
    return False


async def _handle_callback_query(cb: dict) -> None:
    """inline 按钮回调：角色切换 / 模型切换 / message_user 按钮回调解码。"""
    chat_id = cb["message"]["chat"]["id"]
    uid = cb["from"]["id"]
    mid = cb["message"]["message_id"]
    sel = cb["data"]

    if str(uid) != str(chat_id):
        await _send_via_send_message(chat_id, "❌ <b>无权限</b>", reply_message_id=mid)
        await _answer_callback_query(cb["id"], "无权限")
        return

    # 按钮点击也是用户活动：重置主动唤醒的空闲计时
    # （不打断 TIMER 回合——按钮交互不产生新的 USER 模型回合）
    try:
        await proactive.note_user_activity(chat_id, private=True)
    except Exception as e:
        logger.warning(f"note_user_activity(callback) 异常: {e}")

    try:
        if sel in SUPPORTED_ROLES:
            # 锁的粒度只到单个 chat："读角色→切换→回读→更新列表消息
            # （含 Telegram API 网络往返）"整个序列必须串行，保证
            # 同一个 chat 快速连点时序列不交叉；但若用进程级锁包住，
            # 网络往返时间会把锁一直占着，导致其他用户此刻的
            # /adduser /model /role 等操作全部排队等待。因此用按
            # chat_id 分片的 get_chat_lock，不影响其他 chat。
            lock = await get_chat_lock(chat_id)
            async with lock:
                prev = await get_user_role(chat_id)
                if prev == sel:
                    await set_user_role(chat_id, None)
                    notice = "已取消角色设定"
                else:
                    await set_user_role(chat_id, sel)
                    rn = {"china": "中国", "think": "思考", "neko_catgirl": "猫娘", "succubus": "魅魔", "isla": "Isla"}.get(sel, sel)
                    notice = f"已切换到: <b>{rn}</b>"
                cr = await get_user_role(chat_id)
                if role_message_ids.get(chat_id) == mid:
                    ok = await update_role_list(chat_id, mid, SUPPORTED_ROLES, cr)
                else:
                    ok = False
                if not ok:
                    nm = await send_role_list(chat_id, SUPPORTED_ROLES, cr, mid)
                    if nm:
                        role_message_ids[chat_id] = nm
            # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置
            await _send_via_send_message(chat_id, f"✅ <b>{notice}</b>", reply_message_id=mid)
        elif sel in SUPPORTED_MODELS:
            await _answer_callback_query(cb["id"], f"切换到 {SUPPORTED_MODELS[sel].name}...")
            await safe_set_user_model(chat_id, sel)
            model_name = SUPPORTED_MODELS[sel].name
            confirmation = (
                f"✅ <b>模型切换成功</b>\n已切换到模型：<b>{model_name}</b>\n<i>（对话历史已保留）</i>"
            )
            # 使用 sendMessage 避免在 AI 生成中挤占活跃草稿的位置。
            # 快速连点时，本次点击的列表消息可能已被 delete_after
            # 定时清理删除；reply 到已不存在的消息会被 Telegram 以 400
            # 拒绝，若静默吞错会导致"模型已切换却没有任何反馈"。
            # 因此失败后去掉 reply 补发一次，确保切换结果总能送达。
            sent_mid = await _send_via_send_message(chat_id, confirmation, reply_message_id=mid)
            if not sent_mid:
                await _send_via_send_message(chat_id, confirmation)
            # 仿照 /role：点击后不删除列表，而是就地给当前模型
            # 打 √，列表仍由发送时的 delete_after 定时清理。列表消失前
            # 的每一次点击都会得到 "√ 移动 + ✅ 消息" 双重反馈。
            try:
                # get_user_role 内部已用 state._role_lock 保护，
                # 无需再套进程级锁。
                current_role = await get_user_role(chat_id)
                banner_html = ""
                if current_role:
                    banner_html = f"⚠️ <b>兼容性提示</b>\n当前角色「<b>{current_role}</b>」可能与新模型不兼容，请留意。"
                # 读取当前模型与就地更新必须在同一把 chat 锁内完成：
                # 快速连点时多个回调并发执行，若读与编辑分离，后完成的
                # 编辑可能携带旧值，使列表上的 √ 停留在已被覆盖的模型上。
                lock = await get_chat_lock(chat_id)
                async with lock:
                    await update_model_list(
                        chat_id, mid, list(SUPPORTED_MODELS.keys()),
                        get_user_model(chat_id), banner_html,
                    )
            except Exception as list_err:
                logger.warning(f"模型列表就地更新失败(可忽略): chat={chat_id} msg={mid} {list_err}")
            return
        elif isinstance(sel, str) and sel.startswith("ask:"):
            parts = sel.split(":", 3)
            interaction_id = parts[1] if len(parts) > 1 else ""
            action = parts[2] if len(parts) > 2 else ""
            arg = parts[3] if len(parts) > 3 else ""
            ok, notice = await resolve_message_user_callback(
                chat_id, uid, interaction_id, action, arg
            )
            await _answer_callback_query(cb["id"], notice[:200], show_alert=not ok)
            return
        else:
            await _answer_callback_query(cb["id"], "未知操作")
            return
    except Exception as e:
        logger.exception(f"Callback query error: {e}")
        await _send_via_send_message(chat_id, f"❌ <b>操作失败</b>\n<code>{str(e)[:100]}</code>", reply_message_id=mid)
        await _answer_callback_query(cb["id"], "操作失败")
        return

    await _answer_callback_query(cb["id"], "已处理")
    return
