import json
import os
import re
import logging
import aiohttp
from collections import defaultdict
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 配置 OpenRouter API
OPENROUTER_API_KEY = "REDACTED_OPENROUTER_API_KEY"
try:
    openrouter_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    logger.info("OpenRouter API 初始化成功")
except Exception as e:
    logger.error(f"OpenRouter API 初始化失败: {str(e)}")
    raise SystemExit("请检查 API 密钥和网络连接")


def simplify_content(content):
    """将复杂文本内容简化为纯文本，保留原始表情符号"""
    if not content:
        return "[无文本内容]"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result = ""
        for item in content:
            if isinstance(item, str):
                result += item
            elif isinstance(item, dict):
                if item.get("type") == "text_link":
                    result += f"{item['text']} ({item['href']})"
                elif item.get("type") == "custom_emoji":
                    result += item.get('text', '')  # 直接保留表情符号
                elif item.get("type") in ["bold", "code", "hashtag"]:
                    result += item['text']
                else:
                    result += str(item.get('text', ''))
        return result.replace('<br>', ' ').strip() or "[无文本内容]"
    return str(content)


def extract_numeric_id(user_id):
    """提取用户ID的数字部分"""
    if user_id:
        match = re.search(r'\d+', user_id)
        return match.group(0) if match else user_id
    return user_id


def clean_chat_data_to_files(json_data, output_dir='user_chats'):
    """清洗 JSON 数据并按用户生成对话文件，同时生成用户名和ID映射，仅对未生成文件的用户进行导出"""
    try:
        data = json.loads(json_data) if isinstance(json_data, str) else json_data
        logger.info("成功解析 JSON 数据")
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {str(e)}")
        return False, []

    message_to_user = {}
    for message in data.get('messages', []):
        if 'from_id' in message and 'id' in message:
            message_to_user[message['id']] = {
                'username': message.get('from', '已注销账号') or '已注销账号',
                'datetime': message['date']
            }

    users_data = defaultdict(lambda: {'username': '', 'messages': []})

    for message in data.get('messages', []):
        if message.get('type') != 'message':
            continue
        raw_user_id = message.get('from_id')
        user_id = extract_numeric_id(raw_user_id)
        username = message.get('from', '已注销账号') or '已注销账号'
        if not user_id:
            logger.warning(f"消息 {message.get('id', '未知ID')} 缺少用户ID，跳过")
            continue
        if username == '已注销账号':
            logger.warning(f"用户ID {user_id} 的用户名缺失，设为 '已注销账号'")

        users_data[user_id]['username'] = username
        timestamp = message['date']

        reply_info = ""
        if 'reply_to_message_id' in message:
            reply_id = message['reply_to_message_id']
            if reply_id in message_to_user:
                replied_user = message_to_user[reply_id]['username']
                replied_time = message_to_user[reply_id]['datetime'].split('T')[1]
                reply_info = f" (回复 {replied_user} [{replied_time}])"

        text_content = simplify_content(message.get('text', ''))
        if 'file' in message or 'photo' in message:
            media_type = message.get('media_type', '文件')
            text_content += f" [附带 {media_type}]"

        msg_text = f"[{timestamp}]: {text_content}{reply_info}"
        users_data[user_id]['messages'].append({'text': msg_text, 'datetime': message['date']})

    if not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir)
            logger.info(f"创建输出目录: {output_dir}")
        except Exception as e:
            logger.error(f"创建输出目录失败: {str(e)}")
            return False, []

    user_list = []
    user_mapping = {}
    for user_id, user_info in users_data.items():
        filename = os.path.join(output_dir, f"{user_id}.txt")
        # 检查文件是否已存在
        if os.path.exists(filename):
            logger.info(f"文件 {filename} 已存在，跳过导出")
            user_list.append({'username': user_info['username'], 'user_id': user_id})
            user_mapping[user_info['username']] = user_id
            continue

        output_text = f"用户ID: {user_id}\n用户名: {user_info['username']}\n对话:\n"
        sorted_messages = sorted(user_info['messages'], key=lambda x: x['datetime'])
        output_text += "\n".join(msg['text'] for msg in sorted_messages)
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(output_text)
            logger.info(f"已为 {user_info['username']} (ID: {user_id}) 创建文件: {filename}")
            user_list.append({'username': user_info['username'], 'user_id': user_id})
            user_mapping[user_info['username']] = user_id
        except Exception as e:
            logger.error(f"写入文件 {filename} 失败: {str(e)}")

    mapping_file = 'user_mapping.json'
    # 检查映射文件是否已存在，仅在必要时更新
    if not os.path.exists(mapping_file) or user_mapping:
        try:
            with open(mapping_file, 'w', encoding='utf-8') as f:
                json.dump(user_mapping, f, ensure_ascii=False, indent=4)
            logger.info(f"用户名和ID映射已保存到: {mapping_file}")
        except Exception as e:
            logger.error(f"生成映射文件失败: {str(e)}")

    return True, user_list


def load_and_clean_to_user_files(input_filename='result.json', output_dir='user_chats'):
    """从文件加载并清洗数据"""
    try:
        with open(input_filename, 'r', encoding='utf-8') as f:
            input_data = json.load(f)
        success, user_list = clean_chat_data_to_files(input_data, output_dir)
        if success:
            print(f"所有用户对话已从 {input_filename} 分割并保存到 {output_dir} 目录下")
            return user_list
        else:
            print("清理失败，请检查日志")
            return []
    except FileNotFoundError:
        logger.error(f"找不到文件: {input_filename}")
        print(f"错误：找不到文件 {input_filename}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"文件 {input_filename} 不是有效的JSON格式: {str(e)}")
        print(f"错误：文件 {input_filename} 不是有效的JSON格式 - {str(e)}")
        return []
    except Exception as e:
        logger.error(f"发生错误: {str(e)}")
        print(f"发生错误：{str(e)}")
        return []


def build_system_prompt(num_users):
    if num_users == 1:
        return """
        你是一位对话分析专家，目标是对单个用户的对话记录进行深入、数据驱动的剖析。请基于具体证据提供客观见解，分析以下维度：

        - 用户特点：
          - 性格推断：根据语言风格（如正式/非正式）、词汇偏好和语气，推测用户性格（如外向、谨慎、幽默），引用至少 2 个具体对话。
          - 情感基调：分析用户的情绪倾向（如积极、消极）及其变化，标注触发点（如回复某人后情绪转变）。
        - 行为模式：
          - 活跃度：统计消息频率（每日/每小时消息数）和主要活跃时段（含时间范围，如“晚上 8-10 点”）。
          - 交互习惯：评估回复速度（平均延迟时间）、主动性（发起话题比例），并说明是否偏好文本或多媒体。
        - 意图推测：
          - 核心目标：结合上下文推断用户参与对话的目的（如社交、信息获取），引用至少 1 个关键对话。
          - 隐含动机：挖掘潜在需求（如寻求关注、解决问题），说明推理依据。

        输出格式（简洁但完整）：
        - 用户特点：[性格+情感，含例子]
        - 行为模式：[频率+习惯，含数据]
        - 意图推测：[目标+动机，含依据]
        - 总结：[150 字内，概括画像并预测行为趋势]
        """
    else:
        return """
        你是一位对话分析专家，任务是剖析多个用户的对话记录，揭示群体互动的模式与关系。仅通过明确回复（含ID或上下文）确认交互，其他情况需基于时间和内容相关性推测。分析以下维度：

        - 交互频率：
          - 统计每对用户的回复次数，列出前 3 高频互动对，附 1 个对话示例/对。
          - 描述交互的时间集中度（如某日密集或均匀分布）。
        - 关系类型：
          - 判断每对用户的关系（如合作、问答、对抗），基于语气和内容，引用 2 个对话支持。
          - 分析主导性（谁更主动，是否对等），说明依据。
        - 群体动态：
          - 角色识别：根据话题发起和响应频率，划分角色（如主导者、支持者），引用证据。
          - 话题控制：统计谁推动主要话题（消息占比），谁常响应。

        输出格式（清晰且有据）：
        - 交互频率：[用户对+次数+示例]
        - 关系类型：[类型+主导性+例子]
        - 群体动态：[角色+话题分析+证据]
        - 总结：[200 字内，描绘关系网和未来趋势]
        """


def get_user_conversation(username_or_id, chat_dir='user_chats', user_list=None):
    """通过用户名或用户ID查找对话记录"""
    if not user_list:
        logger.error("用户列表为空，无法进行映射查找")
        return None

    for user in user_list:
        if username_or_id.lower() == user['username'].lower() or username_or_id == user['user_id']:
            filepath = os.path.join(chat_dir, f"{user['user_id']}.txt")
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                logger.error(f"读取文件 {filepath} 失败: {str(e)}")
                return None

    logger.warning(f"未找到匹配的用户: {username_or_id}")
    return None


def escape_html_safe(text: str) -> str:
    """安全的 HTML 转义，只转义非 Telegram 支持的标签"""
    if not text:
        return ""
    supported_tags = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
    result = []
    i = 0
    while i < len(text):
        if text[i] == "<":
            j = i + 1
            while j < len(text) and text[j] != ">":
                j += 1
            if j < len(text):
                tag = text[i+1:j].split()[0].strip("/")
                if tag in supported_tags:
                    result.append(text[i:j+1])
                    i = j + 1
                else:
                    result.append(text[i:j+1].replace("<", "&lt;").replace(">", "&gt;"))
                    i = j + 1
            else:
                result.append("<")
                i += 1
        else:
            if text[i] == ">":
                result.append("&gt;")
            else:
                result.append(text[i])
            i += 1
    return "".join(result)


def fix_html_tags(text: str) -> str:
    """修复不完整的 HTML 标签（简单实现）"""
    return text.replace("<br/>", "\n").replace("<br>", "\n")


def estimate_tokens(text: str) -> int:
    """粗略估计令牌数，基于字符数（中文 1 字 ≈ 1 令牌，英文 4 字符 ≈ 1 令牌）"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if ord(c) > 127)
    other_chars = len(text) - chinese_chars
    return chinese_chars + (other_chars // 4) + (1 if other_chars % 4 else 0)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10),
       retry=retry_if_exception_type(Exception))
async def analyze_conversation(users_data):
    """使用 OpenRouter API 分析对话，支持 reasoning 字段输出"""
    num_users = len(users_data)
    system_prompt = build_system_prompt(num_users)
    messages = [{"role": "system", "content": system_prompt}]
    conversation_text = "\n\n".join(users_data)
    messages.append({"role": "user", "content": f"以下是对话记录：\n{conversation_text}"})

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    total_tokens = estimate_tokens(system_prompt) + estimate_tokens(conversation_text)
    use_cache = total_tokens >= 1024
    logger.debug(f"总令牌数: {total_tokens}, 是否启用缓存: {use_cache}")

    model = "qwen/qwq-32b:free"
    reasoning_param = {"max_tokens": 4000}

    if use_cache:
        for msg in messages:
            msg["cache_control"] = {"type": "ephemeral"}
        logger.debug("启用缓存：总令牌数 >= 1024")
    else:
        logger.debug("禁用缓存：总令牌数 < 1024")

    payload = {
        "model": model,
        "messages": messages,
        "reasoning": reasoning_param
    }
    logger.info(f"发送的请求参数: model={model}, payload={json.dumps(payload, ensure_ascii=False)}")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
            if response.status != 200:
                logger.error(f"API 请求失败: {await response.text()}")
                raise Exception(f"API 请求失败，状态码: {response.status}")
            completion_data = await response.json()
            logger.info(f"API 完整响应: {json.dumps(completion_data, ensure_ascii=False)}")

    content = completion_data["choices"][0]["message"]["content"]
    reasoning_text = completion_data["choices"][0]["message"].get("reasoning", None)

    logger.debug(f"AI完整响应内容: {content}")
    if reasoning_text:
        logger.debug(f"AI推理过程: {reasoning_text}")

    def safe_escape_reasoning(text):
        """完全转义思考过程中的所有HTML标签，防止格式化"""
        if not text:
            return ""
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace('"', "&quot;").replace("'", "&#39;").replace("\\n", "\n")
        return text

    reasoning_text_escaped = safe_escape_reasoning(reasoning_text) if reasoning_text else ""
    content_escaped = fix_html_tags(escape_html_safe(content))

    if not content:
        logger.warning("AI 返回空内容，可能生成失败")
        return "⚠️ AI 生成内容为空，请重试"

    if reasoning_text and reasoning_text.strip() and reasoning_text.strip() != "No reasoning provided":
        full_output = (
            "💭 <b>思考过程</b>:\n"
            "<blockquote expandable>" + reasoning_text_escaped + "</blockquote>\n"
            "\n🔍 <b>最终答案</b>:\n" + content_escaped
        )
    else:
        full_output = content_escaped

    logger.debug(f"处理后的完整输出: {full_output}")
    return full_output


def interactive_analysis(chat_dir='user_chats', user_list=None):
    """交互式分析对话"""
    if not user_list:
        print("用户列表为空，请先运行清理步骤")
        return

    print("可用用户：")
    for user in user_list:
        print(f"- {user['username']}:{user['user_id']}")

    while True:
        user_input = input("\n请输入用户名或用户ID（输入 'exit' 退出程序）：").strip()
        if user_input.lower() == 'exit':
            print("程序已退出")
            break
        user1_data = get_user_conversation(user_input, chat_dir, user_list)
        if not user1_data:
            print(f"未找到用户 {user_input} 的对话记录，请检查输入")
            continue
        users_data = [user1_data]
        print(f"已加载用户 {user_input} 的对话")

        add_second = input("是否加入第二个用户的对话？(y/n)：").lower().strip()
        if add_second == 'y':
            user2_input = input("请输入第二个用户名或用户ID：").strip()
            user2_data = get_user_conversation(user2_input, chat_dir, user_list)
            if not user2_data:
                print(f"未找到用户 {user2_input} 的对话记录，请检查输入")
                continue
            users_data.append(user2_data)
            print(f"已加载用户 {user2_input} 的对话")

        try:
            import asyncio
            result = asyncio.run(analyze_conversation(users_data))
            print("\n分析结果：")
            print(result)
        except Exception as e:
            print(f"分析失败：{str(e)}，请稍后重试")
        print("\n提示：输入其他用户继续分析，或输入 'exit' 退出")


if __name__ == "__main__":
    user_list = load_and_clean_to_user_files('result.json', 'user_chats')
    if user_list:
        interactive_analysis('user_chats', user_list)