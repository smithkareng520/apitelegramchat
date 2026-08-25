"""原生图片/视频生成模型的请求与响应解析。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import json
import aiohttp
import base64
import re
import mimetypes
import time
from typing import Optional, Any

from apitelegramchat.config import (
    OPENROUTER_API_KEY,
    AGNES_API_KEY,
    MODELSCOPE_API_KEY,
)
from apitelegramchat.utils import get_logger, strip_html_tags
from apitelegramchat.ai._constants import OPENROUTER_PROVIDER_PREFERENCES
from apitelegramchat.ai.error_formatting import _extract_error_details

logger = get_logger(__name__)

def _clean_prompt_for_image_model(prompt: str) -> str:
    """Strip UI metadata from prompts before image generation."""
    if not prompt:
        return ""
    text = prompt
    text = re.sub(r"^\s*📎\s*用户上传了图片「[^」]*」\s*[\r\n]*", "", text)
    text = re.sub(r"^\s*用户上传了图片「[^」]*」\s*[\r\n]*", "", text)
    return text.strip()


async def _request_modelscope_native_image(
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
) -> tuple[dict | None, str, str, int, str]:
    """
    返回: (response_json, endpoint, error_detail, status_code, request_id)
    - 若服务直接返回图片结果，则 response_json 为最终结果。
    - 若先返回 task_id，则会自动轮询任务结果后再返回最终 JSON。
    """
    base_url = "https://api-inference.modelscope.cn/v1"
    # 关键修复：ModelScope 的图生图（image-to-image）同样走 /images/generations 端点，
    # /images/edits 在 ModelScope API-Inference 上不存在（返回 404 page not found）。
    # 区分文生图与图生图的是 X-ModelScope-Task-Type 头部，而非 URL 路径。
    # 参考实现: https://github.com/hujuying/ComfyUI-ModelScope-API/blob/main/modelscope_image_node.py
    endpoint = "/images/generations"
    request_url = f"{base_url}{endpoint}"

    # 关键修复：ModelScope 异步图像接口要求在 POST 与轮询 GET 上分别附带
    # X-ModelScope-Async-Mode / X-ModelScope-Task-Type 头部，否则任务虽然
    # 在 POST 时返回 task_id=SUCCEED，但 GET /tasks/{task_id} 会立即返回
    # {"errors":{"code":500,"message":"task not found"}, "task_status":"FAILED"}。
    task_type_post = "image-to-image-generation" if image_urls else "text-to-image-generation"
    base_headers = {
        "Authorization": f"Bearer {MODELSCOPE_API_KEY}",
    }
    post_headers = {
        **base_headers,
        "X-ModelScope-Async-Mode": "true",
        "X-ModelScope-Task-Type": task_type_post,
    }
    # 轮询接口使用的 Task-Type 与 POST 不同（参考实现均为 image_generation）
    poll_headers = {
        **base_headers,
        "X-ModelScope-Task-Type": "image_generation",
    }
    # 默认 headers 保留向后兼容（_post_or_get_json 内部已切换为显式传 headers）
    headers = base_headers
    timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=180)

    # 预先清理 prompt，用于日志和后续 payload 构造
    pre_clean_prompt = _clean_prompt_for_image_model(prompt)
    logger.debug(
        "[NativeImage/ModelScope] request prepared: endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) image_count=%s prompt_preview=%r",
        endpoint,
        model,
        len(pre_clean_prompt or ""),
        len(prompt or ""),
        len(image_urls or []),
        (pre_clean_prompt or "")[:240],
    )

    async def _download_image_bytes_from_url(session: aiohttp.ClientSession, image_url: str) -> bytes | None:
        if not image_url:
            return None
        if image_url.startswith("data:image"):
            try:
                _, base64_data = image_url.split(",", 1)
                return base64.b64decode(base64_data)
            except Exception as e:
                logger.warning(f"[NativeImage] data URL 解码失败: {e}")
                return None
        try:
            async with session.get(image_url, timeout=30) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"[NativeImage] 下载参考图失败 {resp.status}: {image_url[:120]}")
        except Exception as e:
            logger.warning(f"[NativeImage] 下载参考图异常: {e}")
        return None

    def _normalize_image_url(image_url: str) -> str:
        return str(image_url or '').strip()

    def _bytes_to_data_url(img_bytes: bytes, source_url: str = '') -> str:
        """把图片字节转换为 data URL（用于 ModelScope 图生图接口的 `image` 字段）。

        - 若 source_url 本身就是 data:image/... URL，则直接返回（已是正确格式）。
        - 否则根据 source_url 的扩展名推断 MIME，缺省 image/jpeg，再用 base64 包装。
        """
        if source_url.startswith('data:image/'):
            return source_url
        mime = 'image/jpeg'
        if source_url:
            guess, _ = mimetypes.guess_type(source_url.split('?', 1)[0])
            if guess and guess.startswith('image/'):
                mime = guess
        b64 = base64.b64encode(img_bytes).decode('ascii')
        return f"data:{mime};base64,{b64}"

    def _body_preview(body_text: str, limit: int = 3000) -> str:
        body_text = body_text or ""
        return body_text[:limit]

    def _safe_json_parse(body_text: str) -> dict | None:
        try:
            parsed = json.loads(body_text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError as e:
            # 仅在 debug 级别输出，避免噪声；但留下诊断痕迹，
            # 此前是完全静默（except Exception: return None），
            # 导致 200 响应体不是合法 JSON 时排查非常困难。
            logger.debug(
                "[NativeImage/ModelScope] JSON parse failed: %s; body_preview=%r",
                e,
                (body_text or "")[:200],
            )
            return None
        except Exception:
            return None

    async def _post_or_get_json(
            session: aiohttp.ClientSession,
            method: str,
            url: str,
            *,
            data=None,
            json_payload=None,
            request_headers: dict | None = None,
            quiet: bool = False,
    ) -> tuple[dict | None, int, str, str]:
        effective_headers = request_headers if request_headers is not None else headers
        # aiohttp 不允许同时给 data 与 json：两者行为未定义，且若 json_payload
        # 不为 None，data 通常会被忽略。这里显式二选一，避免歧义。
        if json_payload is not None:
            async with session.request(method, url, headers=effective_headers, json=json_payload) as resp:
                return await _finalize_response(resp, method, url, quiet)
        else:
            async with session.request(method, url, headers=effective_headers, data=data) as resp:
                return await _finalize_response(resp, method, url, quiet)

    async def _finalize_response(resp, method: str, url: str, quiet: bool) -> tuple[dict | None, int, str, str]:
        """Common response handling extracted from _post_or_get_json for clarity."""
        body_text = await resp.text()
        if not quiet:
            logger.debug(
                "[NativeImage/ModelScope] %s %s response: status=%s content_type=%s body_preview=%r",
                method,
                url.replace(base_url, ''),
                resp.status,
                resp.headers.get("Content-Type", ""),
                _body_preview(body_text),
            )
        request_id = ''
        parsed = _safe_json_parse(body_text)
        if parsed and isinstance(parsed, dict):
            request_id = str(parsed.get('request_id') or parsed.get('requestId') or '').strip()
        if resp.status != 200:
            detail, req_id = _extract_error_details(body_text)
            return None, resp.status, detail or body_text, req_id or request_id
        if parsed is not None:
            if not quiet:
                logger.debug(
                    "[NativeImage/ModelScope] parsed JSON keys=%s",
                    list(parsed.keys())[:40],
                )
            return parsed, resp.status, '', request_id
        if not quiet:
            logger.debug(
                "[NativeImage/ModelScope] JSON parse failed; body_preview=%r",
                _body_preview(body_text),
            )
        return None, resp.status, body_text[:500], request_id

    def _extract_request_meta(payload: dict | None) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return '', ''
        request_id = str(payload.get('request_id') or payload.get('requestId') or '').strip()
        task_id = str(payload.get('task_id') or payload.get('taskId') or '').strip()
        return request_id, task_id

    async with aiohttp.ClientSession(timeout=timeout) as session:
        clean_prompt = pre_clean_prompt
        if image_urls:
            image_data_urls: list[str] = []
            for image_url in image_urls:
                normalized = _normalize_image_url(image_url)
                if not normalized:
                    continue
                if normalized.startswith('data:image/'):
                    image_data_urls.append(normalized)
                elif normalized.startswith('http://') or normalized.startswith('https://'):
                    img_bytes = await _download_image_bytes_from_url(session, normalized)
                    if img_bytes:
                        image_data_urls.append(_bytes_to_data_url(img_bytes, normalized))
                    else:
                        logger.warning("[NativeImage/ModelScope] 下载参考图失败，跳过: %s", normalized[:80])
                else:
                    logger.warning("[NativeImage/ModelScope] 无法识别的 image_url 格式，跳过: %s", normalized[:80])
            if not image_data_urls:
                return None, endpoint, "未能读取参考图片", 400, ""

            payload = {
                "model": model,
                "prompt": clean_prompt or "请根据参考图进行编辑。",
                "image_url": image_data_urls,
                "n": max(1, min(num_images, 4)),
            }
            response_json, status_code, error_detail, request_id = await _post_or_get_json(
                session, "POST", request_url, json_payload=payload, request_headers=post_headers, quiet=True
            )
            if response_json is None:
                logger.warning(
                    "[NativeImage/ModelScope] POST failed: status=%s detail=%s",
                    status_code, (error_detail or '')[:300],
                )
                return None, endpoint, error_detail, status_code, request_id
            _post_task_id = str(response_json.get('task_id') or '').strip()
            _post_task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
            logger.debug(
                "[NativeImage/ModelScope] POST ok: status=200 task_status=%s task_id=%s",
                _post_task_status or 'UNKNOWN', _post_task_id or '-',
            )
        else:
            payload = {
                "model": model,
                "prompt": clean_prompt or "请生成一张图片。",
                "n": max(1, min(num_images, 4)),
            }
            response_json, status_code, error_detail, request_id = await _post_or_get_json(
                session,
                "POST",
                request_url,
                json_payload=payload,
                request_headers=post_headers,
                quiet=True,
            )
            if response_json is None:
                logger.warning(
                    "[NativeImage/ModelScope] POST failed: status=%s detail=%s",
                    status_code, (error_detail or '')[:300],
                )
                return None, endpoint, error_detail, status_code, request_id
            _post_task_id = str(response_json.get('task_id') or '').strip()
            _post_task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
            logger.debug(
                "[NativeImage/ModelScope] POST ok: status=200 task_status=%s task_id=%s",
                _post_task_status or 'UNKNOWN', _post_task_id or '-',
            )

        direct_items = _extract_image_items(response_json)
        if direct_items:
            logger.debug("[NativeImage/ModelScope] direct image payload found, item_count=%s", len(direct_items))
            return response_json, endpoint, '', 200, _extract_request_meta(response_json)[0] or request_id

        task_status = str(response_json.get('task_status') or response_json.get('status') or '').upper()
        task_id = str(response_json.get('task_id') or response_json.get('taskId') or '').strip()
        request_id = _extract_request_meta(response_json)[0] or request_id
        if task_id:
            logger.debug(
                "[NativeImage/ModelScope] task response detected: task_status=%s task_id=%s",
                task_status or 'UNKNOWN',
                task_id,
            )
            # SSRF 防御：task_id 来自上游 API 响应，必须严格白名单后再拼到 URL。
            # 此前是直接 `f"{base_url}/tasks/{task_id}"`，若上游被攻陷或返回
            # 包含 `../` / `?` / host 注入字符串的 task_id，会改写最终的
            # poll_url，把 bot 引导到任意主机。这里要求 task_id 仅包含
            # `[A-Za-z0-9_-]`，长度 1-128，其他一律拒绝并直接返回失败。
            if not re.match(r'^[A-Za-z0-9_-]{1,128}$', task_id):
                logger.warning(
                    "[NativeImage/ModelScope] rejected suspicious task_id=%r",
                    task_id[:32],
                )
                return None, endpoint, "上游返回了非法的 task_id", 200, request_id
            poll_url = f"{base_url}/tasks/{task_id}"
            poll_deadline = time.monotonic() + 240
            poll_interval = 3.0
            poll_max_interval = 5.0
            poll_start = time.monotonic()
            last_poll_json = response_json
            not_found_count = 0
            poll_iter = 0
            await asyncio.sleep(1.5)
            IN_PROGRESS_STATES = {'PENDING', 'PROCESSING', 'RUNNING', 'QUEUED', 'QUEUING', 'STARTED'}

            while time.monotonic() < poll_deadline:
                poll_iter += 1
                poll_json, poll_status, poll_error, poll_request_id = await _post_or_get_json(
                    session, "GET", poll_url, request_headers=poll_headers, quiet=True
                )

                if poll_json is None:
                    if poll_status not in (404, 405):
                        logger.debug(
                            "[NativeImage/ModelScope] polling iter=%s failed: status=%s detail=%s",
                            poll_iter, poll_status, (poll_error or '')[:200],
                        )
                    await asyncio.sleep(poll_interval)
                    poll_interval = min(poll_max_interval, poll_interval + 0.5)
                    continue

                last_poll_json = poll_json
                request_id = poll_request_id or request_id
                poll_task_status = str(poll_json.get('task_status') or poll_json.get('status') or '').upper()
                elapsed = time.monotonic() - poll_start

                errors_obj = poll_json.get('errors')
                err_message = ''
                if isinstance(errors_obj, dict):
                    err_message = str(errors_obj.get('message') or '').strip()
                if not err_message:
                    err_message = str(
                        poll_json.get('message') or poll_json.get('error') or poll_json.get('detail') or ''
                    ).strip()

                if poll_task_status in IN_PROGRESS_STATES or not poll_task_status:
                    logger.debug(
                        "[NativeImage/ModelScope] polling iter=%s status=%s elapsed=%.1fs (next poll in %.1fs)",
                        poll_iter, poll_task_status or 'UNKNOWN', elapsed, poll_interval,
                    )
                    await asyncio.sleep(poll_interval)
                    poll_interval = min(poll_max_interval, poll_interval + 0.5)
                    continue

                poll_items = _extract_image_items(poll_json)
                if poll_items:
                    logger.debug(
                        "[NativeImage/ModelScope] polling succeeded: iter=%s status=%s item_count=%s elapsed=%.1fs",
                        poll_iter, poll_task_status, len(poll_items), elapsed,
                    )
                    return poll_json, endpoint, '', 200, request_id

                is_not_found = (
                        'task not found' in err_message.lower()
                        or err_message.lower().find('not found') >= 0
                        or poll_status == 404
                )

                if poll_task_status in {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED'}:
                    if is_not_found and elapsed < 30:
                        not_found_count += 1
                        logger.debug(
                            "[NativeImage/ModelScope] transient 'task not found' (count=%s elapsed=%.1fs), retrying",
                            not_found_count, elapsed,
                        )
                        await asyncio.sleep(poll_interval)
                        poll_interval = min(poll_max_interval, poll_interval + 0.5)
                        continue
                    detail = err_message or '任务执行失败'
                    return None, endpoint, detail, 200, request_id

                logger.debug(
                    "[NativeImage/ModelScope] polling iter=%s status=%s but no images extracted, elapsed=%.1fs",
                    poll_iter, poll_task_status, elapsed,
                )
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_max_interval, poll_interval + 0.5)

            logger.debug(
                "[NativeImage/ModelScope] task polling timed out after %.1fs (iters=%s); returning last task JSON keys=%s",
                time.monotonic() - poll_start, poll_iter,
                list(last_poll_json.keys())[:40] if isinstance(last_poll_json, dict) else type(last_poll_json).__name__,
            )
            return last_poll_json, endpoint, '', 200, request_id

        return response_json, endpoint, '', 200, request_id


def _extract_image_items(response_json: dict) -> list[dict]:
    """
    从 ModelScope / OpenRouter 等响应中提取图片 URL 或 base64。
    支持递归遍历，处理 output_images, images, results, data, choices 等字段。
    """
    if not isinstance(response_json, dict):
        return []

    items = []
    seen = set()

    def _looks_like_image_payload(value: str) -> bool:
        if not value:
            return False
        value = value.strip()
        return value.startswith(('http://', 'https://', 'data:image/'))

    def _push_item(item):
        if item is None:
            return
        if isinstance(item, str):
            value = item.strip()
            if value and _looks_like_image_payload(value):
                sig = ('url', value)
                if sig not in seen:
                    seen.add(sig)
                    items.append({'image_url': {'url': value}})
            return
        if isinstance(item, dict):
            sig = ('dict', json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
            if sig not in seen:
                seen.add(sig)
                items.append(item)

    def _walk(obj, path='root'):
        if len(items) >= 20:
            return
        if isinstance(obj, dict):
            # 显式处理常见图片字段的字符串列表
            for key in ('output_images', 'images', 'results', 'data', 'choices', 'output'):
                value = obj.get(key)
                if isinstance(value, list):
                    for idx, elem in enumerate(value):
                        if isinstance(elem, str):
                            _push_item(elem)
                        else:
                            _walk(elem, f'{path}.{key}[{idx}]')
                elif isinstance(value, dict):
                    _walk(value, f'{path}.{key}')

            # 处理 base64 字段
            for key in ('b64_json', 'base64', 'image_base64'):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    _push_item({'b64_json': value.strip()})

            # 处理 URL 字段
            for key in ('url', 'image_url', 'output_image', 'image_uri'):
                value = obj.get(key)
                if isinstance(value, dict):
                    url = str(value.get('url') or value.get('href') or value.get('link') or '').strip()
                    if url:
                        _push_item({'image_url': {'url': url}})
                elif isinstance(value, str):
                    value = value.strip()
                    if _looks_like_image_payload(value):
                        _push_item({'image_url': {'url': value}})

            # 递归其他嵌套字段（跳过已处理的）
            for key, value in obj.items():
                if key in {'output_images', 'images', 'results', 'data', 'choices', 'content',
                           'url', 'image_url', 'output_image', 'image_uri',
                           'b64_json', 'base64', 'image_base64'}:
                    continue
                if isinstance(value, (dict, list)):
                    _walk(value, f'{path}.{key}')

            # 处理 content 列表（OpenAI 格式）
            content = obj.get('content')
            if isinstance(content, list):
                for idx, part in enumerate(content):
                    if isinstance(part, dict):
                        ptype = str(part.get('type') or '').lower()
                        if ptype in ('image_url', 'image', 'file'):
                            if isinstance(part.get('image_url'), dict):
                                url = str(part['image_url'].get('url') or '').strip()
                                if url:
                                    _push_item({'image_url': {'url': url}})
                            elif isinstance(part.get('image_url'), str):
                                url = part['image_url'].strip()
                                if url:
                                    _push_item({'image_url': {'url': url}})
                            elif isinstance(part.get('url'), str):
                                url = part['url'].strip()
                                if url:
                                    _push_item({'image_url': {'url': url}})

        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                _walk(item, f'{path}[{idx}]')
        elif isinstance(obj, str):
            _push_item(obj)

    # 先处理顶层常见字段
    for key in ('data', 'choices', 'output', 'results', 'images', 'output_images'):
        value = response_json.get(key)
        if isinstance(value, (dict, list)):
            _walk(value, f'root.{key}')
        elif isinstance(value, str):
            _push_item(value)

    # 全量递归（兜底）
    _walk(response_json)
    return items


def _extract_native_message_text(content: Any) -> str:
    """尽量从模型内容中提取纯文本，兼容字符串与结构化 content。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                piece = item.strip()
                if piece:
                    parts.append(piece)
                continue
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type in ("text", "output_text"):
                piece = item.get("text") or item.get("output_text") or item.get("content")
                if isinstance(piece, str) and piece.strip():
                    parts.append(piece.strip())
            elif isinstance(item.get("text"), str) and item.get("text").strip():
                parts.append(item.get("text").strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_native_refusal_text(message: Any) -> str:
    """提取模型拒绝文本，兼容 refusal 字段、dump 结构与字符串化对象。"""
    if message is None:
        return ""
    refusal = getattr(message, "refusal", None)
    if refusal is None:
        try:
            msg_dump = message.model_dump()
            refusal = msg_dump.get("refusal")
        except Exception:
            refusal = None
    if isinstance(refusal, str):
        return refusal.strip()
    if isinstance(refusal, list):
        return "\n".join(str(x) for x in refusal if x is not None).strip()
    if refusal is not None:
        return str(refusal).strip()
    return ""


def _format_native_image_notice(
        *,
        content_text: str = "",
        refusal_text: str = "",
        finish_reason: str = "",
) -> str:
    """把模型拒绝/安全拦截/仅文本输出，统一转成用户可见友好提示。"""
    content_text = strip_html_tags(content_text or "").strip()
    refusal_text = strip_html_tags(refusal_text or "").strip()
    reason = (finish_reason or "").strip().lower()

    if reason in {"content_filter", "safety", "blocked", "moderation"}:
        if refusal_text:
            return f"⚠️ 这张图触发了安全限制：{refusal_text[:600]}"
        if content_text:
            return f"⚠️ 这张图触发了安全限制：{content_text[:600]}"
        return "⚠️ 这张图触发了安全限制，请修改描述后重试。"

    if refusal_text:
        return f"⚠️ {refusal_text[:600]}"

    if content_text:
        return content_text[:1200]

    return "⚠️ 图片生成失败，请稍后重试。"


async def _response_items_to_bytes(response_json: dict) -> list[bytes]:
    image_bytes_list: list[bytes] = []
    items = _extract_image_items(response_json)
    logger.debug("[NativeImage/ModelScope] extracted image item count=%s", len(items))
    # 防止恶意/失控的上游用超大 base64 串触发 OOM：
    # 单张图片的 base64 串超过 25 MB 时直接拒绝解码。
    MAX_BASE64_ENCODED_BYTES = 25 * 1024 * 1024
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for img_data in items:
            img_url = ''
            if isinstance(img_data.get('image_url'), dict):
                img_url = str(img_data['image_url'].get('url') or '').strip()
            elif isinstance(img_data.get('image_url'), str):
                img_url = str(img_data.get('image_url') or '').strip()
            img_url = img_url or str(img_data.get('url') or '').strip()
            b64_json = str(img_data.get('b64_json') or img_data.get('base64') or '').strip()

            if b64_json:
                if len(b64_json) > MAX_BASE64_ENCODED_BYTES:
                    logger.warning(
                        "[NativeImage] 跳过超大 base64 图片 (len=%s, 上限=%s)",
                        len(b64_json), MAX_BASE64_ENCODED_BYTES,
                    )
                    continue
                try:
                    image_bytes_list.append(base64.b64decode(b64_json))
                    continue
                except Exception as e:
                    logger.warning(f"[NativeImage] Base64 图片解码失败: {e}")

            if img_url.startswith('data:image'):
                try:
                    _, base64_data = img_url.split(',', 1)
                    if len(base64_data) > MAX_BASE64_ENCODED_BYTES:
                        logger.warning(
                            "[NativeImage] 跳过超大 data URL 图片 (len=%s, 上限=%s)",
                            len(base64_data), MAX_BASE64_ENCODED_BYTES,
                        )
                        continue
                    image_bytes_list.append(base64.b64decode(base64_data))
                    continue
                except Exception as e:
                    logger.warning(f"[NativeImage] data URL 解码失败: {e}")

            if img_url.startswith('http'):
                try:
                    async with session.get(img_url, timeout=30) as resp:
                        if resp.status == 200:
                            # 同样限制远端下载体积，避免恶意 upstream 用
                            # 一个 100 MB 的"图片"把进程拖垮。
                            max_remote = 25 * 1024 * 1024
                            image_bytes = await resp.content.read(max_remote + 1)
                            if len(image_bytes) > max_remote:
                                logger.warning(
                                    "[NativeImage] 远端图片体积超限 (>%s)，跳过: %s",
                                    max_remote, img_url[:120],
                                )
                                continue
                            image_bytes_list.append(image_bytes)
                        else:
                            logger.warning(f"[NativeImage] 下载生成图片失败 {resp.status}: {img_url[:120]}")
                except Exception as e:
                    logger.warning(f"[NativeImage] 下载生成图片异常: {e}")
    return image_bytes_list


async def _request_agnes_video(
        prompt: str,
        duration: int,
        model: str,
) -> tuple[str | None, str | None, Optional[dict]]:
    """
    提交视频任务到 Agnes 并轮询结果。
    返回 (video_url, error_message, meta)；成功时 error=None，meta 含 width/height/frame_rate/num_frames 等元数据。
    """
    base_url = "https://apihub.agnes-ai.com/v1"
    headers = {
        "Authorization": f"Bearer {AGNES_API_KEY}",
        "Content-Type": "application/json",
    }

    clean_prompt = (prompt or "").strip()
    logger.debug(
        "[NativeVideo/Agnes] request prepared: model=%s duration=%ss prompt_len=%s prompt_preview=%r",
        model,
        duration,
        len(clean_prompt),
        clean_prompt[:240],
    )

    # 提交任务
    submit_url = f"{base_url}/videos"
    payload = {
        "model": model,
        "prompt": clean_prompt,
        "duration": duration,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(submit_url, headers=headers, json=payload, timeout=30) as resp:
                resp_text = await resp.text()
                logger.debug(
                    "[NativeVideo/Agnes] submit response: status=%s content_type=%s body_preview=%r",
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp_text[:500],
                )
                if resp.status != 200:
                    return None, f"Agnes 提交失败 (HTTP {resp.status}): {resp_text[:200]}", None

                try:
                    data = json.loads(resp_text)
                except Exception:
                    return None, f"Agnes 提交返回非 JSON: {resp_text[:200]}", None

                video_id = data.get("video_id") or data.get("id")
                if not video_id:
                    logger.warning("[NativeVideo/Agnes] submit ok but no video_id in response keys=%s",
                                   list(data.keys())[:40])
                    return None, "Agnes 响应中没有 video_id", None

                logger.debug(
                    "[NativeVideo/Agnes] submit ok: video_id=%s keys=%s",
                    str(video_id),
                    list(data.keys())[:40],
                )

    except Exception as e:
        logger.exception("[NativeVideo/Agnes] submit exception")
        return None, f"Agnes 提交异常: {str(e)[:100]}", None

    # 轮询结果
    poll_url = "https://apihub.agnes-ai.com/agnesapi"
    max_wait = 300  # 5分钟
    interval = 3
    start_time = time.monotonic()
    poll_iter = 0

    logger.debug(
        "[NativeVideo/Agnes] start polling: poll_url=%s max_wait=%ss interval=%ss video_id=%s",
        poll_url,
        max_wait,
        interval,
        video_id,
    )

    async with aiohttp.ClientSession() as session:
        while time.monotonic() - start_time < max_wait:
            poll_iter += 1
            elapsed = time.monotonic() - start_time

            try:
                params = {"video_id": video_id}
                async with session.get(poll_url, headers=headers, params=params, timeout=15) as resp:
                    body_text = await resp.text()
                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s response: status=%s body_preview=%r",
                        poll_iter,
                        resp.status,
                        body_text[:500],
                    )

                    if resp.status != 200:
                        logger.debug(
                            "[NativeVideo/Agnes] polling iter=%s non-200 status=%s elapsed=%.1fs",
                            poll_iter,
                            resp.status,
                            elapsed,
                        )
                        await asyncio.sleep(interval)
                        continue

                    try:
                        data = json.loads(body_text)
                    except Exception:
                        logger.warning(
                            "[NativeVideo/Agnes] polling iter=%s JSON parse failed elapsed=%.1fs body_preview=%r",
                            poll_iter,
                            elapsed,
                            body_text[:300],
                        )
                        await asyncio.sleep(interval)
                        continue

                    status = str(data.get("status") or "").lower()
                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s status=%s elapsed=%.1fs keys=%s",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        list(data.keys())[:40],
                    )

                    if status == "completed":
                        video_url = (
                                data.get("video_url")
                                or data.get("url")
                                or (data.get("output") or {}).get("url")
                        )
                        if video_url:
                            # 同时上报 perf_params 作为视频元数据（用于 caption）
                            meta = data.get("perf_params") or {}
                            logger.info(
                                "[NativeVideo/Agnes] polling succeeded: iter=%s elapsed=%.1fs video_url=%r",
                                poll_iter,
                                elapsed,
                                str(video_url)[:240],
                            )
                            return video_url, None, meta

                        logger.warning(
                            "[NativeVideo/Agnes] completed but missing video_url: iter=%s elapsed=%.1fs keys=%s",
                            poll_iter,
                            elapsed,
                            list(data.keys())[:40],
                        )
                        return None, "Agnes 任务完成但未返回视频 URL", None

                    if status in ("failed", "error"):
                        error_msg = data.get("error") or data.get("message") or "未知错误"
                        logger.error(
                            "[NativeVideo/Agnes] polling failed: iter=%s elapsed=%.1fs error=%r",
                            poll_iter,
                            elapsed,
                            str(error_msg)[:300],
                        )
                        return None, f"Agnes 视频生成失败: {error_msg}", None

                    logger.debug(
                        "[NativeVideo/Agnes] polling iter=%s still running: status=%s elapsed=%.1fs next_poll_in=%ss",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        interval,
                    )
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.warning(
                    "[NativeVideo/Agnes] polling exception: iter=%s elapsed=%.1fs err=%s",
                    poll_iter,
                    elapsed,
                    str(e)[:200],
                )
                await asyncio.sleep(interval)
                continue

    logger.warning("[NativeVideo/Agnes] polling timeout: max_wait=%ss video_id=%s", max_wait, video_id)
    return None, f"Agnes 轮询超时 ({max_wait} 秒)", None


async def _request_openrouter_video(
        prompt: str,
        duration: int,
        model: str,
) -> tuple[str | None, str | None, Optional[dict]]:
    """
    提交视频任务到 OpenRouter 并轮询结果。
    返回 (video_url, error_message, meta)；成功时 error=None，meta 通常为 None（OpenRouter 不暴露同量级的元数据）。
    """
    base_url = "https://openrouter.ai/api/v1"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    clean_prompt = (prompt or "").strip()
    logger.debug(
        "[NativeVideo/OpenRouter] request prepared: model=%s duration=%ss prompt_len=%s prompt_preview=%r",
        model,
        duration,
        len(clean_prompt),
        clean_prompt[:240],
    )

    # 提交任务
    submit_url = f"{base_url}/videos"
    payload = {
        "model": model,
        "prompt": clean_prompt,
        "duration": duration,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "provider": OPENROUTER_PROVIDER_PREFERENCES,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(submit_url, headers=headers, json=payload, timeout=30) as resp:
                resp_text = await resp.text()
                logger.debug(
                    "[NativeVideo/OpenRouter] submit response: status=%s content_type=%s body_preview=%r",
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp_text[:500],
                )

                if resp.status != 202:
                    return None, f"OpenRouter 提交失败 (HTTP {resp.status}): {resp_text[:200]}", None

                try:
                    data = json.loads(resp_text)
                except Exception:
                    return None, f"OpenRouter 提交返回非 JSON: {resp_text[:200]}", None

                job_id = data.get("id")
                polling_url = data.get("polling_url") or data.get("status_url")

                if not job_id or not polling_url:
                    logger.warning(
                        "[NativeVideo/OpenRouter] submit ok but missing job_id/polling_url keys=%s",
                        list(data.keys())[:40],
                    )
                    return None, "OpenRouter 响应缺少 job_id 或 polling_url", None

                # SSRF 防御：polling_url 由上游 API 返回，恶意/被攻陷的
                # 上游可让 bot 去访问内网（如 169.254.169.254 metadata
                # endpoint 或 127.0.0.1）。这里强制白名单只允许
                # openrouter.ai 主机，其它一律拒绝。
                try:
                    from urllib.parse import urlparse
                    _parsed_poll = urlparse(str(polling_url))
                except Exception:
                    _parsed_poll = None
                if (
                    not _parsed_poll
                    or _parsed_poll.scheme not in ("http", "https")
                    or _parsed_poll.netloc.lower() not in {"openrouter.ai", "api.openrouter.ai"}
                ):
                    logger.warning(
                        "[NativeVideo/OpenRouter] rejected polling_url outside allowlist: %r",
                        str(polling_url)[:200],
                    )
                    return None, "OpenRouter 返回了不在白名单内的轮询 URL", None

                logger.debug(
                    "[NativeVideo/OpenRouter] submit ok: job_id=%s polling_url=%s keys=%s",
                    str(job_id),
                    str(polling_url)[:240],
                    list(data.keys())[:40],
                )

    except Exception as e:
        logger.exception("[NativeVideo/OpenRouter] submit exception")
        return None, f"OpenRouter 提交异常: {str(e)[:100]}", None

    # 轮询结果
    max_wait = 300
    interval = 3
    start_time = time.monotonic()
    poll_iter = 0

    logger.debug(
        "[NativeVideo/OpenRouter] start polling: max_wait=%ss interval=%ss job_id=%s polling_url=%s",
        max_wait,
        interval,
        str(job_id),
        str(polling_url)[:240],
    )

    async with aiohttp.ClientSession() as session:
        while time.monotonic() - start_time < max_wait:
            poll_iter += 1
            elapsed = time.monotonic() - start_time

            try:
                async with session.get(polling_url, headers=headers, timeout=15) as resp:
                    body_text = await resp.text()
                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s response: status=%s body_preview=%r",
                        poll_iter,
                        resp.status,
                        body_text[:500],
                    )

                    if resp.status != 200:
                        logger.debug(
                            "[NativeVideo/OpenRouter] polling iter=%s non-200 status=%s elapsed=%.1fs",
                            poll_iter,
                            resp.status,
                            elapsed,
                        )
                        await asyncio.sleep(interval)
                        continue

                    try:
                        data = json.loads(body_text)
                    except Exception:
                        logger.warning(
                            "[NativeVideo/OpenRouter] polling iter=%s JSON parse failed elapsed=%.1fs body_preview=%r",
                            poll_iter,
                            elapsed,
                            body_text[:300],
                        )
                        await asyncio.sleep(interval)
                        continue

                    status = str(data.get("status") or "").lower()
                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s status=%s elapsed=%.1fs keys=%s",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        list(data.keys())[:40],
                    )

                    if status == "completed":
                        video_url = data.get("content") or (
                            data.get("unsigned_urls", [None])[0] if isinstance(data.get("unsigned_urls"),
                                                                               list) else None
                        )
                        if video_url:
                            logger.info(
                                "[NativeVideo/OpenRouter] polling succeeded: iter=%s elapsed=%.1fs video_url=%r",
                                poll_iter,
                                elapsed,
                                str(video_url)[:240],
                            )
                            return video_url, None, None

                        logger.warning(
                            "[NativeVideo/OpenRouter] completed but missing video_url: iter=%s elapsed=%.1fs keys=%s",
                            poll_iter,
                            elapsed,
                            list(data.keys())[:40],
                        )
                        return None, "OpenRouter 任务完成但未返回视频 URL", None

                    if status in ("failed", "error"):
                        error_msg = data.get("error") or data.get("message") or "未知错误"
                        logger.error(
                            "[NativeVideo/OpenRouter] polling failed: iter=%s elapsed=%.1fs error=%r",
                            poll_iter,
                            elapsed,
                            str(error_msg)[:300],
                        )
                        return None, f"OpenRouter 视频生成失败: {error_msg}", None

                    logger.debug(
                        "[NativeVideo/OpenRouter] polling iter=%s still running: status=%s elapsed=%.1fs next_poll_in=%ss",
                        poll_iter,
                        status or "unknown",
                        elapsed,
                        interval,
                    )
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.warning(
                    "[NativeVideo/OpenRouter] polling exception: iter=%s elapsed=%.1fs err=%s",
                    poll_iter,
                    elapsed,
                    str(e)[:200],
                )
                await asyncio.sleep(interval)
                continue

    logger.warning("[NativeVideo/OpenRouter] polling timeout: max_wait=%ss job_id=%s", max_wait, str(job_id))
    return None, f"OpenRouter 轮询超时 ({max_wait} 秒)", None


