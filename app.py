from quart import Quart, request
import asyncio
import aiohttp
import json
from utils import send_message, send_list_with_timeout, delete_message, escape_html, check_deepseek_balance, \
    check_openrouter_balance
from ai_handlers import get_ai_response
from config import BASE_URL, WEBHOOK_URL, SUPPORTED_MODELS, AUTHORIZED_USER, TELEGRAM_BOT_TOKEN, global_lock, user_role_selections
from file_handlers import parse_file
import re
import logging

app = Quart(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_contexts = {}
user_models = {}
media_groups = {}
processed_updates = set()
role_message_ids = {}  # 存储每个 chat_id 的角色列表消息 ID

MAX_CHARS = 120000
MEDIA_GROUP_TIMEOUT = 5
ROLE_LIST_TIMEOUT = 10  # 设置角色选择消息的自动删除时间为 10 秒

async def set_webhook() -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/setWebhook", json={"url": WEBHOOK_URL}) as response:
            if response.status == 200:
                logger.info("[INIT] Webhook configured successfully")
            else:
                logger.error(f"[ERROR] Webhook setup failed: {await response.text()}")

async def trim_conversation_history(chat_id: int, new_message: dict) -> None:
    """Add message to history and trim"""
    async with global_lock:
        if chat_id not in user_contexts:
            user_contexts[chat_id] = {"conversation_history": [], "search_mode": False}
        history = user_contexts[chat_id]["conversation_history"]

        if "content" in new_message and isinstance(new_message["content"], str):
            content = new_message["content"]
            logger.debug(f"Processing message content: {content[:100]}...")

            if "<pre>" in content:
                content = re.sub(
                    r'<pre>(.*?)</pre>',
                    lambda m: f'<pre>{escape_html(m.group(1))}</pre>',
                    content,
                    flags=re.DOTALL
                )
                logger.debug("Cleaned code blocks in message")

            if "🔍 <b>最终答案</b>:" in content:
                content = content.split("🔍 <b>最终答案</b>:")[-1].strip()
            elif "💭 <b>思考过程</b>:" in content:
                content = content.split("\n🔍 <b>最终答案</b>:")[-1].strip() if "🔍 <b>最终答案</b>:" in content else ""

            new_message["content"] = content

        history.append(new_message)
        total_chars = sum(len(msg["content"]) for msg in history if isinstance(msg["content"], str))

        while total_chars > MAX_CHARS and history:
            removed = history.pop(0)
            logger.debug(f"Removed message from history (length: {len(removed.get('content', ''))})")
            total_chars = sum(len(msg["content"]) for msg in history if isinstance(msg["content"], str))

        user_contexts[chat_id]["conversation_history"] = history[-50:]
        logger.debug(
            f"Updated conversation history: {len(history)} messages, latest: {new_message.get('content', '')[:50]}...")

async def process_media_group(chat_id: int, media_group_id: str) -> None:
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    async with global_lock:
        if media_group_id not in media_groups:
            return
        messages = media_groups.pop(media_group_id)
        user_contexts[chat_id]["search_mode"] = False
        current_model = user_models.get(chat_id, "grok-2-vision-latest")
        model_info = SUPPORTED_MODELS.get(current_model, {})
        supports_vision = model_info.get("vision", False)

    contents = []
    file_ids = []
    user_input = ""
    for msg in messages:
        photo_sizes = msg["photo"]
        file_id = photo_sizes[-1]["file_id"]
        file_name = f"photo_{file_id}.jpg"
        if supports_vision:
            file_ids.append(file_id)
        else:
            content = await parse_file(file_id, file_name)
            if content:
                contents.append(content)
        if "caption" in msg and not user_input:
            user_input = msg["caption"].strip()

    if not supports_vision and not contents:
        await send_message(chat_id, "❌ All image parsing failed", max_chars=4000, pre_escaped=False)
        return

    if supports_vision:
        user_message = {
            "role": "user",
            "content": user_input or "Please analyze these images",
            "file_ids": file_ids,
            "type": "photo_group"
        }
    else:
        photo_header = "📸 <b>Image Contents</b>:<br><br>"
        combined_content = "<br><br>".join(f"Image {i + 1}:<br>{content}" for i, content in enumerate(contents))
        user_message = {
            "role": "user",
            "content": f"{user_input}<br><br>{photo_header}{combined_content}" if user_input else f"{photo_header}Please analyze these images:<br>{combined_content}"
        }

    full_response, clean_content = await get_ai_response(chat_id, user_models, user_contexts, user_message=user_message)
    if full_response == "IMAGE_SENT":
        await trim_conversation_history(chat_id, user_message)
        assistant_message = {"role": "assistant", "content": clean_content.strip()}
        await trim_conversation_history(chat_id, assistant_message)
    elif full_response and not full_response.startswith("⏳") and not full_response.startswith("⚠️"):
        await trim_conversation_history(chat_id, user_message)
        await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
        assistant_message = {"role": "assistant", "content": clean_content.strip()}
        await trim_conversation_history(chat_id, assistant_message)
    else:
        await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)

async def send_role_list_with_timeout(chat_id: int, role_list: list, current_role: str) -> int:
    """Send role list message with timeout and return message_id"""
    formatted_roles = []
    for role in role_list:
        if role == current_role:
            formatted_roles.append(f"{role} √")
        else:
            formatted_roles.append(role)

    keyboard = {
        "inline_keyboard": [
            [{"text": role_text, "callback_data": role}] for role_text, role in zip(formatted_roles, role_list)
        ]
    }
    payload = {
        "chat_id": chat_id,
        "text": "选择角色设定 (再次点击取消):",
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }
    message_id = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BASE_URL}/sendMessage", json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    message_id = result.get("result", {}).get("message_id")
                    if message_id:
                        await asyncio.sleep(ROLE_LIST_TIMEOUT)  # 等待指定时间后删除
                        await delete_message(chat_id, message_id)
                        async with global_lock:
                            if chat_id in role_message_ids and role_message_ids[chat_id] == message_id:
                                del role_message_ids[chat_id]  # 清理 role_message_ids
                else:
                    logger.error(f"Failed to send role list: {await response.text()}")
                    await send_message(chat_id, "❌ 无法显示角色列表，请重试", max_chars=4000, pre_escaped=False)
    except Exception as e:
        logger.error(f"Error in send_role_list_with_timeout: {str(e)}")
    return message_id

@app.route('/webhook', methods=['POST'])
async def webhook() -> tuple:
    try:
        received_token = request.args.get("token")
        from config import WEBHOOK_TOKEN
        if not received_token or received_token != WEBHOOK_TOKEN:
            logger.warning(f"Webhook token 验证失败: 接收到的 token={received_token}")
            return "Forbidden: Invalid or missing token", 403

        data = await request.json
        update_id = data.get('update_id')
        logger.info(f"[REQUEST] Received update: {update_id}")

        async with global_lock:
            if update_id in processed_updates:
                logger.info(f"[INFO] Update {update_id} already processed, skipping")
                return "OK", 200
            processed_updates.add(update_id)

        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            username = data["message"]["from"].get("username")

            async with global_lock:
                if chat_id not in user_contexts:
                    user_contexts[chat_id] = {"conversation_history": [], "search_mode": False}
                if chat_id not in user_models:
                    user_models[chat_id] = "grok-2-vision-latest"

            if "media_group_id" in data["message"] and "photo" in data["message"]:
                media_group_id = data["message"]["media_group_id"]
                async with global_lock:
                    if media_group_id not in media_groups:
                        media_groups[media_group_id] = []
                        asyncio.create_task(process_media_group(chat_id, media_group_id))
                    media_groups[media_group_id].append(data["message"])
                return "OK", 200

            elif "photo" in data["message"]:
                async with global_lock:
                    user_contexts[chat_id]["search_mode"] = False
                    current_model = user_models.get(chat_id, "grok-2-vision-latest")
                    model_info = SUPPORTED_MODELS.get(current_model, {})
                    supports_vision = model_info.get("vision", False)

                photo_sizes = data["message"]["photo"]
                file_id = photo_sizes[-1]["file_id"]
                file_name = f"photo_{file_id}.jpg"
                user_input = data["message"].get("caption", "").strip()

                if supports_vision:
                    user_message = {
                        "role": "user",
                        "content": user_input or "Please analyze this image",
                        "file_id": file_id,
                        "type": "photo"
                    }
                else:
                    content = await parse_file(file_id, file_name)
                    if content is None:
                        await send_message(chat_id, "❌ Image parsing failed", max_chars=4000, pre_escaped=False)
                        return "OK", 200
                    photo_header = "📸 <b>Image Content</b>:<br><br>"
                    user_message = {
                        "role": "user",
                        "content": f"{user_input}<br><br>{photo_header}{content}" if user_input else f"{photo_header}Please analyze this image:<br>{content}"
                    }

                full_response, clean_content = await get_ai_response(chat_id, user_models, user_contexts, user_message=user_message)
                if full_response == "IMAGE_SENT":
                    await trim_conversation_history(chat_id, user_message)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                elif full_response and not full_response.startswith("⏳") and not full_response.startswith("⚠️"):
                    await trim_conversation_history(chat_id, user_message)
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                else:
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                return "OK", 200

            elif "document" in data["message"]:
                async with global_lock:
                    user_contexts[chat_id]["search_mode"] = False
                    current_model = user_models.get(chat_id, "grok-2-vision-latest")
                    model_info = SUPPORTED_MODELS.get(current_model, {})
                    supports_document = model_info.get("document", False)

                file_id = data["message"]["document"]["file_id"]
                file_name = data["message"]["document"]["file_name"]
                user_input = data["message"].get("caption", "").strip()

                if supports_document:
                    user_message = {
                        "role": "user",
                        "content": user_input or "Please analyze this document",
                        "file_id": file_id,
                        "type": "document"
                    }
                else:
                    content = await parse_file(file_id, file_name)
                    if content is None:
                        await send_message(chat_id, "❌ File parsing failed or unsupported file type", max_chars=4000, pre_escaped=False)
                        return "OK", 200
                    file_header = f"📄 <b>Filename</b>: <code>{file_name}</code><br><br>"
                    user_message = {
                        "role": "user",
                        "content": f"{user_input}<br><br>{file_header}File content:<br>{content}" if user_input else f"{file_header}Please analyze this file:<br>{content}"
                    }

                full_response, clean_content = await get_ai_response(chat_id, user_models, user_contexts, user_message=user_message)
                if full_response == "IMAGE_SENT":
                    await trim_conversation_history(chat_id, user_message)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                elif full_response and not full_response.startswith("⏳") and not full_response.startswith("⚠️"):
                    await trim_conversation_history(chat_id, user_message)
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                else:
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                return "OK", 200

            elif "text" in data["message"]:
                user_input = data["message"]["text"]

                if user_input.startswith("/start"):
                    welcome_message = """
                    <b>Welcome to AI Assistant!</b> 😊

                    <b>Commands:</b>
                    - <code>/model</code>: Switch AI models (use grok-2-image for images)
                    - <code>/role</code>: Select role persona (catgirl or succubus)
                    - <code>/clear</code>: Clear chat history
                    - <code>/search</code>: Toggle search mode
                    - <code>/balance [service]</code>: Check API balance
                      • No args or <code>all</code>: Show all balances
                      • <code>deepseek</code> or <code>ds</code>: DeepSeek only
                      • <code>openrouter</code> or <code>or</code>: OpenRouter only

                    <b>Features:</b>
                    - Upload multiple images/files supported
                    """
                    await send_message(chat_id, welcome_message, max_chars=4000, pre_escaped=False)
                    return "OK", 200

                elif user_input.startswith("/role"):
                    role_list = ["neko_catgirl", "succubus"]
                    async with global_lock:
                        current_role = user_role_selections.get(chat_id, None)
                        # 发送角色列表并设置自动删除
                        message_id = await send_role_list_with_timeout(chat_id, role_list, current_role)
                        if message_id:
                            role_message_ids[chat_id] = message_id
                    return "OK", 200

                elif user_input.startswith("/balance"):
                    parts = user_input.split(maxsplit=1)
                    service = parts[1].lower() if len(parts) > 1 else None

                    balance_message_parts = []

                    if not service or service == "all":
                        deepseek_balance, deepseek_currency = await check_deepseek_balance()
                        if deepseek_balance and deepseek_currency:
                            balance_message_parts.append(
                                f"💰 <b>DeepSeek 余额</b>: {deepseek_balance} {deepseek_currency}"
                            )
                        else:
                            balance_message_parts.append("⚠️ <b>DeepSeek</b>: 查询失败")

                        openrouter_balance = await check_openrouter_balance()
                        if openrouter_balance is not None:
                            balance_message_parts.append(
                                f"💰 <b>OpenRouter 余额</b>: ${openrouter_balance:.3f} USD"
                            )
                        else:
                            balance_message_parts.append("⚠️ <b>OpenRouter</b>: 查询失败")

                    elif service in ["deepseek", "ds"]:
                        deepseek_balance, deepseek_currency = await check_deepseek_balance()
                        if deepseek_balance and deepseek_currency:
                            balance_message_parts.append(
                                f"💰 <b>DeepSeek 余额</b>: {deepseek_balance} {deepseek_currency}"
                            )
                        else:
                            balance_message_parts.append("⚠️ <b>DeepSeek</b>: 查询失败")

                    elif service in ["openrouter", "or"]:
                        openrouter_balance = await check_openrouter_balance()
                        if openrouter_balance is not None:
                            balance_message_parts.append(
                                f"💰 <b>OpenRouter 余额</b>: ${openrouter_balance:.3f} USD"
                            )
                        else:
                            balance_message_parts.append("⚠️ <b>OpenRouter</b>: 查询失败")

                    else:
                        balance_message_parts.append(
                            "❌ 无效的服务名称\n可用选项: <code>deepseek</code>, <code>openrouter</code>, <code>all</code>"
                        )

                    balance_message = "\n".join(balance_message_parts)
                    await send_message(chat_id, balance_message, max_chars=4000, pre_escaped=False)
                    return "OK", 200

                elif user_input.startswith("/model"):
                    if data["message"]["chat"]["type"] != "private":
                        await send_message(chat_id, "❌ Model switching only available in private chats", max_chars=4000, pre_escaped=False)
                        return "OK", 200

                    model_list = list(SUPPORTED_MODELS.keys())
                    try:
                        await send_list_with_timeout(chat_id, "Choose a model:", model_list, timeout=8)
                    except Exception as e:
                        logger.error(f"Failed to send model list: {str(e)}")
                        await send_message(chat_id, "❌ Failed to send model list, please try again", max_chars=4000, pre_escaped=False)
                    return "OK", 200

                elif user_input.startswith("/clear"):
                    async with global_lock:
                        user_contexts[chat_id]["conversation_history"] = []
                    await send_message(chat_id, "✅ Conversation history cleared", max_chars=4000, pre_escaped=False)
                    return "OK", 200

                elif user_input.startswith("/search"):
                    async with global_lock:
                        current_mode = user_contexts[chat_id]["search_mode"]
                        user_contexts[chat_id]["search_mode"] = not current_mode
                        if user_contexts[chat_id]["search_mode"]:
                            await send_message(chat_id, "🔍 <b>Search mode enabled</b>. Enter your search query. Use <code>/search</code> again to disable.", max_chars=4000, pre_escaped=False)
                        else:
                            await send_message(chat_id, "✅ <b>Search mode disabled</b>, returning to normal mode.", max_chars=4000, pre_escaped=False)
                    return "OK", 200

                async with global_lock:
                    search_mode = user_contexts[chat_id]["search_mode"]

                user_message = {"role": "user", "content": user_input}
                if search_mode:
                    if not user_input.strip():
                        await send_message(chat_id, "❌ Please provide search content", max_chars=4000, pre_escaped=False)
                        return "OK", 200
                    full_response, clean_content = await get_ai_response(chat_id, user_models, user_contexts, is_search=True, user_message=user_message)
                else:
                    full_response, clean_content = await get_ai_response(chat_id, user_models, user_contexts, user_message=user_message)

                if full_response == "IMAGE_SENT":
                    await trim_conversation_history(chat_id, user_message)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                elif full_response and not full_response.startswith("⏳") and not full_response.startswith("⚠️"):
                    await trim_conversation_history(chat_id, user_message)
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                    assistant_message = {"role": "assistant", "content": clean_content.strip()}
                    await trim_conversation_history(chat_id, assistant_message)
                else:
                    await send_message(chat_id, full_response, max_chars=4000, pre_escaped=True)
                return "OK", 200

        elif "callback_query" in data:
            callback = data["callback_query"]
            chat_id = callback["message"]["chat"]["id"]
            user_id = callback["from"]["id"]
            message_id = callback["message"]["message_id"]
            selected_data = callback["data"]

            if str(user_id) != str(chat_id):
                await send_message(chat_id, "❌ Unauthorized to change other users' settings", max_chars=4000, pre_escaped=False)
                return "OK", 200

            async with global_lock:
                role_list = ["neko_catgirl", "succubus"]
                if selected_data in role_list:
                    current_role = user_role_selections.get(chat_id)
                    if current_role == selected_data:
                        user_role_selections.pop(chat_id, None)
                        role_name = "已取消角色设定"
                    else:
                        user_role_selections[chat_id] = selected_data
                        role_name = f"已切换到: <b>{'猫娘' if selected_data == 'neko_catgirl' else '魅魔'}</b>"
                    
                    # 发送确认消息并立即删除角色选择消息
                    await send_message(chat_id, f"✅ {role_name}", max_chars=4000, pre_escaped=False)
                    await delete_message(chat_id, message_id)
                    if chat_id in role_message_ids and role_message_ids[chat_id] == message_id:
                        del role_message_ids[chat_id]

                elif selected_data in SUPPORTED_MODELS:
                    user_models[chat_id] = selected_data
                    model_name = f"✅ Switched model to: <b>{SUPPORTED_MODELS[selected_data]['name']}</b>"
                    await send_message(chat_id, model_name, max_chars=4000, pre_escaped=False)
                    await delete_message(chat_id, message_id)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{BASE_URL}/answerCallbackQuery",
                        json={"callback_query_id": callback["id"], "text": "Received"}
                ) as response:
                    if response.status != 200:
                        logger.error(f"Callback query response failed: {await response.text()}")

            return "OK", 200

        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return "Internal Server Error", 500

async def main():
    await set_webhook()
    await app.run_task(host="0.0.0.0", port=5000)

if __name__ == '__main__':
    asyncio.run(main())
