"""原生图片/视频生成模型的请求与响应解析。

从 ai_handlers.py 拆分而来，逻辑未做改动。

图像生成统一入口（OpenAI Images 协议）：
所有走 OpenAI Images 协议的提供商（ModelScope / XXTF 等）共用
:func:`_request_images_generations` 一个请求出口——端点选择、鉴权、
请求头、payload 构造、参考图下载、响应解析全部在此合并；厂商差异
（ModelScope 的异步任务轮询、xxtf 的官方 /images/edits multipart 形状）
作为该出口内部的分支处理，调用方（agentic 原生图像循环 / 工具图像
生成）不再按提供商各写一套。
"""
import asyncio
import json
import aiohttp
import base64
import re
import mimetypes
import time
import uuid
from typing import Optional, Any

from config import (
    OPENROUTER_API_KEY,
    AGNES_API_KEY,
    MODELSCOPE_API_KEY,
    PROVIDERS,
    get_effective_endpoint,
)
from utils import get_logger, strip_html_tags
from ai._constants import OPENROUTER_PROVIDER_PREFERENCES
from ai.error_formatting import _extract_error_details

logger = get_logger(__name__)

# =============================================================================
# 统一图像请求（OpenAI Images 协议 /v1/images/{generations,edits}）
# -----------------------------------------------------------------------------
# 走统一 Images 协议出口的提供商集合。判定依据是"该提供商的图像模型
# 支持 OpenAI Images 兼容端点"，而不是按模型逐个判断：
#   - modelscope: 文生图与图生图共用 /images/generations（用
#     X-ModelScope-Task-Type 头区分；ModelScope 无 /images/edits 端点）
#   - xxtf:       中转站标准 OpenAI Images 端点（gpt-image-2 等），按官方
#     语义分端点：文生图 -> JSON /images/generations；带参考图的编辑 ->
#     官方规范 /images/edits（multipart/form-data，image[] 字段，见
#     developers.openai.com "Create image edit"），中转站未实现该路由时
#     回退 JSON /images/generations + image 字段的兼容形状。另有生产
#     鲁棒性三件套：edits 路由级 404/405 按 base_url 短期缓存（免得每次
#     白打一趟必败请求）、"请求体未完整/请重试"类瞬态 400 同形状自动
#     重试、超大参考图先降采样再上传（详见 _request_openai_compat_image）
# 其它提供商（如 openrouter 的 gemini 图像模型）继续走
# chat/completions + modalities 路径，行为不变。
# =============================================================================
IMAGES_API_PROVIDERS = frozenset({"modelscope", "xxtf"})


def _get_images_api_display_name(model_info) -> str:
    """返回提供商展示名（如 "ModelScope" / "XXTF"），用于错误提示文案。"""
    provider_key = getattr(model_info, "provider", "") or ""
    base = PROVIDERS.get(provider_key)
    return (getattr(base, "name", "") or provider_key or "图像").strip()


def _resolve_provider_api_key(api_key_env: str) -> str:
    """从 config 模块解析提供商 API Key（与 api_client._get_api_key 同语义）。

    从 config 模块变量（而非 os.environ）读取：scrub_environment() 清洗
    环境变量后应用仍能拿到 key。
    """
    if not api_key_env:
        return ""
    import config as app_config
    return str(getattr(app_config, api_key_env, "") or "")


def _normalize_image_url(image_url: str) -> str:
    return str(image_url or '').strip()


async def _download_reference_image_bytes(session: aiohttp.ClientSession, image_url: str) -> bytes | None:
    """下载参考图字节；data: URL 直接解码，http(s) 下载，其余返回 None。"""
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


def _bytes_to_data_url(img_bytes: bytes, source_url: str = '') -> str:
    """把图片字节转换为 data URL（用于图像编辑接口的参考图字段）。

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


async def _image_urls_to_data_urls(session: aiohttp.ClientSession, image_urls: list[str]) -> list[str]:
    """把调用方给的参考图（data URL / 公网 URL）统一转成 data URL 列表。

    ModelScope 与通用 OpenAI 兼容图像端点的参考图预处理共用本函数：
    下载失败的参考图跳过（全部失败时由调用方返回 400）。
    """
    image_data_urls: list[str] = []
    for image_url in image_urls or []:
        normalized = _normalize_image_url(image_url)
        if not normalized:
            continue
        if normalized.startswith('data:image/'):
            image_data_urls.append(normalized)
        elif normalized.startswith(('http://', 'https://')):
            img_bytes = await _download_reference_image_bytes(session, normalized)
            if img_bytes:
                image_data_urls.append(_bytes_to_data_url(img_bytes, normalized))
            else:
                logger.warning("[NativeImage] 下载参考图失败，跳过: %s", normalized[:80])
        else:
            logger.warning("[NativeImage] 无法识别的 image_url 格式，跳过: %s", normalized[:80])
    return image_data_urls


def _safe_json_parse_dict(body_text: str) -> dict | None:
    """把响应体解析为 dict；非 JSON / 非 dict 返回 None（debug 日志留痕）。"""
    try:
        parsed = json.loads(body_text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError as e:
        logger.debug(
            "[NativeImage] JSON parse failed: %s; body_preview=%r",
            e,
            (body_text or "")[:200],
        )
        return None
    except Exception:
        logger.debug("_safe_json_parse_dict 内部忽略的异常", exc_info=True)
        return None


# OpenAI Images 的 size 参数只认固定档位；把工具侧宽高比映射过去，
# 映射不到的（21:9 / 4:5 / 5:4）返回 None = 不发送，走提供商默认（auto）。
_OPENAI_SIZE_BY_ASPECT_RATIO = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "4:3": "1536x1024",
    "16:9": "1536x1024",
    "2:3": "1024x1536",
    "3:4": "1024x1536",
    "9:16": "1024x1536",
}


def _aspect_ratio_to_openai_size(aspect_ratio: str | None) -> str | None:
    if not aspect_ratio:
        return None
    return _OPENAI_SIZE_BY_ASPECT_RATIO.get(str(aspect_ratio).strip())


async def _upload_generated_images_to_r2(image_bytes_list: list[bytes]) -> list[str]:
    """统一后处理：把生成图片逐张上传 R2，返回成功上传的公网 URL 列表。

    agentic 原生图像循环与工具图像生成（execute_generate_image）此前各写
    一份相同的上传循环，这里合并为单一实现；部分失败不抛异常，由调用方
    根据"上传数 < 生成数"自行决定提示文案。
    """
    # 局部导入避免与 s3_utils 的初始化顺序耦合（s3_utils 依赖 config/R2 配置）
    from s3_utils import upload_bytes_to_r2
    uploaded_urls: list[str] = []
    for idx, img_bytes in enumerate(image_bytes_list or []):
        key = f"generated/{uuid.uuid4().hex}_{idx}.png"
        url = await upload_bytes_to_r2(img_bytes, key, "image/png")
        if url:
            uploaded_urls.append(url)
        else:
            logger.warning("[NativeImage] 一张图片上传 R2 失败")
    return uploaded_urls


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
    ModelScope 专用分支（由统一出口 _request_images_generations 调度）：
    与通用 OpenAI 兼容实现共享端点形状（/images/generations）、参考图
    预处理（_image_urls_to_data_urls）与 JSON 解析（_safe_json_parse_dict），
    仅保留 ModelScope 特有的异步任务头与 /tasks/{task_id} 轮询。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    - 若服务直接返回图片结果，则 response_json 为最终结果。
    - 若先返回 task_id，则会自动轮询任务结果后再返回最终 JSON。
    """
    base_url = "https://api-inference.modelscope.cn/v1"
    # 注意：ModelScope 的图生图（image-to-image）同样走 /images/generations 端点，
    # /images/edits 在 ModelScope API-Inference 上不存在（返回 404 page not found）。
    # 区分文生图与图生图的是 X-ModelScope-Task-Type 头部，而非 URL 路径。
    # 参考实现: https://github.com/hujuying/ComfyUI-ModelScope-API/blob/main/modelscope_image_node.py
    endpoint = "/images/generations"
    request_url = f"{base_url}{endpoint}"

    # 注意：ModelScope 异步图像接口要求在 POST 与轮询 GET 上分别附带
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

    def _body_preview(body_text: str, limit: int = 3000) -> str:
        body_text = body_text or ""
        return body_text[:limit]

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
        parsed = _safe_json_parse_dict(body_text)
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
            # 参考图预处理与通用 OpenAI 兼容端点共用同一实现
            # （data URL 直通，http(s) 下载后转 data URL，失败跳过）
            image_data_urls = await _image_urls_to_data_urls(session, image_urls)
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
            # SSRF 防御：task_id 来自上游 API 响应，必须严格白名单后再拼到
            # URL：若 task_id 包含 `../` / `?` / host 注入字符串，会改写
            # 最终的 poll_url，把 bot 引导到任意主机。这里要求 task_id
            # 仅包含 `[A-Za-z0-9_-]`，长度 1-128，其他一律拒绝并直接返回失败。
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


# ---- 通用 OpenAI 兼容实现的共享辅助（edits multipart / generations JSON 两路共用）----

# data URL 的 MIME -> multipart 文件名扩展名（官方示例用 .png / .jpg / .webp）
_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

# /images/edits 路由级失败 -> 可回退 /images/generations 的状态码。
# 只覆盖"中转站没实现/网关没转发该路由"类错误；鉴权(401/403)、限流(429)、
# 内容安全(400 moderation)等语义性错误不回退，避免重复计费或掩盖真实原因。
_EDITS_FALLBACK_STATUSES = frozenset({404, 405, 501, 502, 503})
# 400 状态下命中这些字样视为"路由/参数未实现"（各家中转报错措辞不一，宽松收集）
_EDITS_FALLBACK_BODY_HINTS = (
    "unknown parameter",
    "unrecognized request argument",
    "unrecognized field",
    "unexpected field",
    "extra fields not permitted",
    "extra_forbidden",
    "invalid field",
    "does not support",
    "not supported",
    "no such endpoint",
    "unknown request",
    "invalid url",
    "page not found",
    "not found",
)


def _should_fallback_to_generations(status_code: int, error_detail: str) -> bool:
    """判定 /images/edits 失败后是否回退 /images/generations JSON 形状。

    仅对"路由未实现 / 参数不识别"类失败回退；内容安全、鉴权、计费类
    错误原样返回给调用方（回退也无法解决，反而多打一次计费请求）。
    """
    if status_code in _EDITS_FALLBACK_STATUSES:
        return True
    if status_code == 400:
        body = (error_detail or "").lower()
        return any(hint in body for hint in _EDITS_FALLBACK_BODY_HINTS)
    return False


# ---- 生产鲁棒性辅助：edits 路由能力缓存 / 瞬态 400 重试 / 超大参考图压缩 ----

# /images/edits 返回 404/405（路由级"确实没有该路径"，如生产实测 xxtf
# 中转站）后按 base_url 记忆一段时间：TTL 内带参考图的请求直接走兼容
# JSON 形状，省去一次必然失败的 RTT。501/502/503 等其它回退状态不缓存
# ——可能是瞬态网关故障，下次仍先按官方语义尝试 edits。
_EDITS_UNSUPPORTED_TTL_SECONDS = 3600.0
_edits_unsupported_until: dict[str, float] = {}


def _mark_edits_unsupported(base_url: str) -> None:
    """记录"该 base_url 未实现 /images/edits"（TTL 内跳过 multipart 尝试）。"""
    if base_url:
        _edits_unsupported_until[base_url] = time.time() + _EDITS_UNSUPPORTED_TTL_SECONDS


def _edits_known_unsupported(base_url: str) -> bool:
    if not base_url:
        return False
    expire_at = _edits_unsupported_until.get(base_url)
    if expire_at is None:
        return False
    if time.time() >= expire_at:
        _edits_unsupported_until.pop(base_url, None)
        return False
    return True


# 瞬态 400：中转站明确提示"请求体未完整/请重试"类传输层错误（生产实测
# xxtf 返回 400 INCOMPLETE_REQUEST_BODY "请求体未完整到达，请重试"，同一
# 请求稍后重试即成功）。这类错误属于 invalid_request_error 且请求未被
# 服务端受理（无计费），同形状重试一次是安全的；内容安全/鉴权类 400
# 不在此列。
_TRANSIENT_400_HINTS = (
    "incomplete_request_body",
    "请求体未完整",
    "请重试",
    "please retry",
    "please try again",
    "try again later",
)
_TRANSIENT_RETRY_BACKOFF_SECONDS = 1.5


def _is_transient_400(status_code: int, error_detail: str) -> bool:
    """判定是否为"中转站明确要求重试"类瞬态 400（非参数/安全语义错误）。"""
    if status_code != 400:
        return False
    body = (error_detail or "").lower()
    return any(hint in body for hint in _TRANSIENT_400_HINTS)


async def _post_images_with_retry(
        session: aiohttp.ClientSession,
        url: str,
        *,
        endpoint: str,
        log_prefix: str,
        headers: dict | None = None,
        json_payload: dict | None = None,
        form_factory=None,
) -> tuple[dict | None, str, str, int, str]:
    """Images 协议 POST 共用出口：响应处理 + 瞬态 400 同形状重试一次。

    json_payload（JSON 形状）与 form_factory（multipart 形状，传入返回
    新建 :class:`aiohttp.FormData` 的可调用对象）二选一。multipart 重试
    必须重建 FormData——bytes 字段的流游标消费后不可复用。
    返回形状与 :func:`_finalize_images_response` 一致。
    """
    result: tuple[dict | None, str, str, int, str] = (None, endpoint, "", 0, "")
    form = form_factory() if form_factory is not None else None
    for attempt in (1, 2):
        async with session.post(
            url, headers=headers, json=json_payload, data=form
        ) as resp:
            result = await _finalize_images_response(resp, endpoint, log_prefix)
        parsed, _, detail, status_code, _req_id = result
        if parsed is not None or not _is_transient_400(status_code, detail):
            return result
        if attempt == 1:
            logger.warning(
                "%s %s 瞬态失败 (status=%s detail=%r)，%.1fs 后同形状重试一次",
                log_prefix, endpoint, status_code, (detail or "")[:120],
                _TRANSIENT_RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SECONDS)
            form = form_factory() if form_factory is not None else None
    return result


# 超大参考图压缩：参考图塞进 multipart（edits）或 JSON（回退形状）时，
# 数 MB 的原图会造出巨大请求体，部分中转站读不满直接 400"请求体未完整
# 到达"。参考图对 gpt-image 系列只是编辑依据，长边 <=2048 / ~3MB 已足够
# （官方输出分辨率本身集中在 1024/1536 档）。
_REF_IMAGE_MAX_BYTES = 3 * 1024 * 1024
_REF_IMAGE_MAX_SIDE = 2048
# JPEG 降档兜底：极端高熵图（如噪声/密集纹理）在 2048/q88 下仍可能超阈值，
# 依次降质量，仍未达标则长边降到 1536 再压。
_REF_IMAGE_JPEG_QUALITIES = (88, 72, 56)
_REF_IMAGE_FALLBACK_SIDE = 1536


def _encode_jpeg_bytes(img, quality: int) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _shrink_reference_bytes(img_bytes: bytes, mime: str) -> tuple[bytes, str]:
    """超过 3MB 的参考图降采样（长边 2048；有 alpha 先保 PNG，仍过大则白底转 JPEG）。

    JPEG 质量降档 88 -> 72 -> 56，仍未达标则长边再降到 1536。
    Pillow 不可用 / 图片解码失败 / 压缩无收益时原样返回，绝不阻断主流程。
    """
    if len(img_bytes) <= _REF_IMAGE_MAX_BYTES:
        return img_bytes, mime
    try:
        from io import BytesIO
        from PIL import Image  # Pillow 已在 requirements.txt（Pillow==11.1.0）
        img = Image.open(BytesIO(img_bytes))
        img.load()
        width, height = img.size
        scale = max(width, height) / _REF_IMAGE_MAX_SIDE
        if scale > 1:
            img = img.resize(
                (max(1, round(width / scale)), max(1, round(height / scale))),
                Image.LANCZOS,
            )
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        if has_alpha:
            png_buf = BytesIO()
            img.save(png_buf, format="PNG", optimize=True)
            if png_buf.tell() <= _REF_IMAGE_MAX_BYTES:
                out_bytes = png_buf.getvalue()
                if len(out_bytes) < len(img_bytes):
                    logger.info(
                        "[NativeImage/OpenAICompat] 超大参考图已压缩: %d -> %d bytes (%s)",
                        len(img_bytes), len(out_bytes), "image/png",
                    )
                    return out_bytes, "image/png"
            # PNG 仍过大：白底合成 alpha 后转入 JPEG 降档
            rgba = img.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.split()[-1])
            img = flattened
        out_bytes = b""
        for quality in _REF_IMAGE_JPEG_QUALITIES:
            out_bytes = _encode_jpeg_bytes(img, quality)
            if len(out_bytes) <= _REF_IMAGE_MAX_BYTES:
                break
        if len(out_bytes) > _REF_IMAGE_MAX_BYTES and max(img.size) > _REF_IMAGE_FALLBACK_SIDE:
            w, h = img.size
            scale2 = max(w, h) / _REF_IMAGE_FALLBACK_SIDE
            img2 = img.resize(
                (max(1, round(w / scale2)), max(1, round(h / scale2))),
                Image.LANCZOS,
            )
            out_bytes = _encode_jpeg_bytes(img2, _REF_IMAGE_JPEG_QUALITIES[-1])
        if out_bytes and len(out_bytes) < len(img_bytes):
            logger.info(
                "[NativeImage/OpenAICompat] 超大参考图已压缩: %d -> %d bytes (%s)",
                len(img_bytes), len(out_bytes), "image/jpeg",
            )
            return out_bytes, "image/jpeg"
    except Exception as e:  # Pillow 缺失 / 图片损坏等：原样返回，不阻断主流程
        logger.debug("[NativeImage/OpenAICompat] 参考图压缩跳过: %s", e)
    return img_bytes, mime


def _shrink_data_url(data_url: str) -> str:
    """对单张 data URL 参考图应用 :func:`_shrink_reference_bytes`。"""
    decoded = _data_url_to_bytes(data_url)
    if not decoded:
        return data_url
    img_bytes, mime = decoded
    new_bytes, new_mime = _shrink_reference_bytes(img_bytes, mime)
    if new_bytes is img_bytes:
        return data_url
    return "data:{};base64,{}".format(
        new_mime, base64.b64encode(new_bytes).decode("ascii")
    )


def _data_url_to_bytes(data_url: str) -> tuple[bytes, str] | None:
    """把 data:image/... URL 解码为 (字节, mime)；失败返回 None。"""
    if not data_url or not data_url.startswith("data:image/"):
        return None
    try:
        header, b64_data = data_url.split(",", 1)
        mime = "image/png"
        m = re.match(r"data:([^;,]+)", header)
        if m and m.group(1).strip():
            mime = m.group(1).strip().lower()
        return base64.b64decode(b64_data), mime
    except Exception as e:
        logger.warning("[NativeImage/OpenAICompat] data URL 解码失败: %s", e)
        return None


async def _finalize_images_response(
        resp: aiohttp.ClientResponse,
        endpoint: str,
        log_prefix: str,
) -> tuple[dict | None, str, str, int, str]:
    """Images 协议两种 POST（JSON / multipart）共用的响应处理。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    """
    body_text = await resp.text()
    logger.debug(
        "%s POST response: status=%s content_type=%s body_preview=%r",
        log_prefix,
        resp.status,
        resp.headers.get("Content-Type", ""),
        body_text[:800],
    )
    if resp.status != 200:
        detail, req_id = _extract_error_details(body_text)
        candidate = detail or body_text
        if candidate.lstrip().startswith("<"):
            # 网关/中转的 HTML 错误页（如 Python http.server 默认 404 页）：
            # 提取纯文本摘要，避免整页 HTML 进入错误提示与日志
            detail = re.sub(r"\s+", " ", strip_html_tags(candidate)).strip()[:300]
        logger.warning(
            "%s POST failed: status=%s detail=%s",
            log_prefix, resp.status, (detail or body_text)[:300],
        )
        return None, endpoint, detail or body_text[:500], resp.status, req_id or ''
    parsed = _safe_json_parse_dict(body_text)
    if parsed is None:
        return None, endpoint, body_text[:500], resp.status, ''
    return parsed, endpoint, '', resp.status, ''


async def _request_openai_compat_image(
        model_info,
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
        aspect_ratio: str | None = None,
) -> tuple[dict | None, str, str, int, str]:
    """通用 OpenAI Images 兼容实现（XXTF 等中转站走这里）。

    端点选择对齐 OpenAI 官方 Images API 语义（developers.openai.com
    api/reference/resources/images/methods/{generate,edit}，2026-09 现场核实）：

    - 无参考图（文生图）: POST JSON ``{base_url}/images/generations``
      （官方 "Create image"，仅接受文本 prompt，无 image 参数）。
    - 有参考图（图生图/编辑）: 官方是独立端点 "Create image edit"
      ``{base_url}/images/edits``，且必须是 multipart/form-data——参考图以
      重复的 ``image[]`` 文件字段逐张上传（gpt-image 系列支持多张），
      文本字段为 model / prompt / n [/ size]：

          curl https://api.openai.com/v1/images/edits \\
            -F "model=gpt-image-1.5" -F "image[]=@a.png" -F "image[]=@b.png" \\
            -F 'prompt=...'

      历史实现曾把参考图塞进 JSON 发到 /images/generations——官方端点
      并不接受 image 参数，只是部分中转站自行兼容。现在按以下顺序：
        1. 先按官方规范 POST multipart /images/edits；
        2. 仅当中转站返回"路由未实现/参数不识别"类错误（见
           :func:`_should_fallback_to_generations`）才回退旧行为:
           POST JSON /images/generations 并携带 image 字段（单张字符串、
           多张数组）。鉴权/内容安全等语义错误不回退、原样返回。
        3. 鲁棒性（生产日志反馈迭代）：/images/edits 收到路由级 404/405 后
           按 base_url 缓存（TTL 内直接回退，省一次必败 RTT）；"请求体
           未完整/请重试"类瞬态 400 同形状自动重试一次；超大参考图
           （>3MB）先降采样再上传，避免中转站读不满请求体。

    - base_url 沿用 provider/模型端点覆盖的合并结果（XXTF 默认
      ``https://xxtf.baby/v1``，即最终请求 ``https://xxtf.baby/v1/images/edits``
      等真实路径）。
    - 鉴权：``Bearer {api_key_env 解析出的 key}``；provider 级
      default_headers（如 XXTF 的浏览器 UA）一并下发。multipart 请求
      不手动设置 Content-Type（aiohttp 自动生成带 boundary 的头）。
    - 响应：两个端点同为 OpenAI 标准 ``{data: [{url} | {b64_json}]}``，
      图片提取复用 :func:`_extract_image_items`（gpt-image 系列固定回
      b64_json，同样覆盖）。

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    endpoint 为实际使用的相对路径（"/images/edits" 或 "/images/generations"），
    调用方拼 /v1 前缀用于展示。
    """
    gen_endpoint = "/images/generations"
    edits_endpoint = "/images/edits"
    log_prefix = "[NativeImage/OpenAICompat]"
    ep = get_effective_endpoint(model_info)
    base_url = (getattr(ep, "base_url", "") or "").rstrip("/")
    api_key_env = getattr(ep, "api_key_env", "") or ""
    api_key = _resolve_provider_api_key(api_key_env)
    if not base_url:
        return None, gen_endpoint, f"提供商 {ep.name!r} 未配置 base_url", 400, ""
    if not api_key:
        return None, gen_endpoint, f"缺少 API Key: {api_key_env}，请设置环境变量", 401, ""

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        **(getattr(ep, "default_headers", None) or {}),
    }
    json_headers = {**auth_headers, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=180)

    clean_prompt = _clean_prompt_for_image_model(prompt)
    size = _aspect_ratio_to_openai_size(aspect_ratio)
    n = max(1, min(num_images, 4))

    def _payload_base(prompt_text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt_text,
            "n": n,
        }
        if size:
            payload["size"] = size
        return payload

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # ---------------- 文生图：JSON /images/generations ----------------
            if not image_urls:
                endpoint = gen_endpoint
                payload = _payload_base(clean_prompt or "请生成一张图片。")
                logger.debug(
                    "%s request prepared: provider=%s endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) prompt_preview=%r",
                    log_prefix, ep.name, endpoint, model,
                    len(clean_prompt or ""), len(prompt or ""),
                    (clean_prompt or "")[:240],
                )
                return await _post_images_with_retry(
                    session, f"{base_url}{endpoint}",
                    endpoint=endpoint, log_prefix=log_prefix,
                    headers=json_headers, json_payload=payload,
                )

            # ------------- 图生图/编辑：官方 /images/edits multipart -------------
            endpoint = edits_endpoint
            image_data_urls = await _image_urls_to_data_urls(session, image_urls)
            if not image_data_urls:
                return None, endpoint, "未能读取参考图片", 400, ""
            # 超大参考图先降采样：数 MB 的 body 塞进 multipart/JSON 会被部分
            # 中转站截断（生产实测 xxtf 400 INCOMPLETE_REQUEST_BODY）
            image_data_urls = [_shrink_data_url(u) for u in image_data_urls]

            logger.debug(
                "%s request prepared: provider=%s endpoint=%s model=%s prompt_len=%s(cleaned, raw=%s) image_count=%s prompt_preview=%r",
                log_prefix, ep.name, endpoint, model,
                len(clean_prompt or ""), len(prompt or ""),
                len(image_data_urls), (clean_prompt or "")[:240],
            )
            # 预解码参考图（一次解码，multipart 构造可重复执行——瞬态重试
            # 时需重建 FormData，bytes 字段的流游标不可复用）
            decoded_refs: list[tuple[int, bytes, str, str]] = []
            for idx, data_url in enumerate(image_data_urls):
                decoded = _data_url_to_bytes(data_url)
                if not decoded:
                    continue
                img_bytes, mime = decoded
                decoded_refs.append((idx, img_bytes, mime, _MIME_TO_EXT.get(mime, "png")))
            if not decoded_refs:
                return None, endpoint, "未能读取参考图片", 400, ""

            def _build_edits_form() -> aiohttp.FormData:
                """官方形状：多张参考图以重复的 image[] 字段上传。"""
                form = aiohttp.FormData()
                form.add_field("model", str(model))
                form.add_field("prompt", clean_prompt or "请根据参考图进行编辑。")
                form.add_field("n", str(n))
                if size:
                    form.add_field("size", str(size))
                for idx, img_bytes, mime, ext in decoded_refs:
                    form.add_field(
                        "image[]", img_bytes,
                        filename=f"reference_{idx}.{ext}", content_type=mime,
                    )
                return form

            edits_skipped = _edits_known_unsupported(base_url)
            if edits_skipped:
                logger.info(
                    "%s %s 近期 /images/edits 404/405 已缓存，跳过直接走 %s 兼容形状",
                    log_prefix, base_url, gen_endpoint,
                )
            if not edits_skipped:
                parsed, used_endpoint, detail, status_code, req_id = (
                    await _post_images_with_retry(
                        session, f"{base_url}{endpoint}",
                        endpoint=endpoint, log_prefix=log_prefix,
                        headers=auth_headers, form_factory=_build_edits_form,
                    )
                )
                if parsed is not None:
                    return parsed, used_endpoint, '', status_code, req_id
                if not _should_fallback_to_generations(status_code, detail):
                    return None, used_endpoint, detail, status_code, req_id
                if status_code in (404, 405):
                    # 路由级"确实没有该路径" -> 按 base_url 短期缓存，
                    # TTL 内后续请求不再白打这一趟
                    _mark_edits_unsupported(base_url)
            else:
                used_endpoint, status_code, detail, req_id = endpoint, 404, "", ''

            # ------- 回退：中转站未实现 /images/edits 时的兼容 JSON 形状 -------
            fallback_payload = _payload_base(clean_prompt or "请根据参考图进行编辑。")
            fallback_payload["image"] = (
                image_data_urls[0] if len(image_data_urls) == 1 else image_data_urls
            )
            logger.warning(
                "%s /images/edits 不可用 (status=%s detail=%r)，回退 %s JSON+image 形状",
                log_prefix, status_code, (detail or '')[:160], gen_endpoint,
            )
            return await _post_images_with_retry(
                session, f"{base_url}{gen_endpoint}",
                endpoint=gen_endpoint, log_prefix=log_prefix,
                headers=json_headers, json_payload=fallback_payload,
            )
    except asyncio.TimeoutError:
        logger.warning("%s request timeout after %ss", log_prefix, timeout.total)
        return None, endpoint, "图像请求超时", 504, ""
    except Exception as e:
        logger.exception("%s request exception", log_prefix)
        return None, endpoint, str(e)[:500], 500, ""


async def _request_images_generations(
        model_info,
        *,
        prompt: str,
        image_urls: list[str],
        num_images: int = 1,
        model: str = "",
        aspect_ratio: str | None = None,
) -> tuple[dict | None, str, str, int, str]:
    """统一图像请求出口：所有 OpenAI Images 协议提供商共用这一个函数。

    调用方（_agentic_loop_native_image / execute_generate_image）不再按
    提供商各写一套请求逻辑，只拿到统一形状的返回值后做各自的呈现：

    - modelscope -> _request_modelscope_native_image（异步任务轮询特化）
    - 其它（xxtf 等中转站）-> _request_openai_compat_image（标准同步 REST；
      文生图走 JSON /images/generations，带参考图的编辑优先走官方
      multipart /images/edits，路由未实现时回退 JSON 形状）

    返回: (response_json, endpoint, error_detail, status_code, request_id)
    endpoint 为实际使用的相对路径（"/images/generations" 或
    "/images/edits"），调用方拼 /v1 前缀用于展示。
    """
    provider = (getattr(model_info, "provider", "") or "").strip().lower()
    if provider == "modelscope":
        return await _request_modelscope_native_image(
            prompt=prompt,
            image_urls=image_urls,
            num_images=num_images,
            model=model,
        )
    return await _request_openai_compat_image(
        model_info,
        prompt=prompt,
        image_urls=image_urls,
        num_images=num_images,
        model=model,
        aspect_ratio=aspect_ratio,
    )


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
            logger.debug("_extract_native_refusal_text 内部忽略的异常", exc_info=True)
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
                    logger.debug("_request_agnes_video 内部忽略的异常", exc_info=True)
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
                    logger.debug("_request_openrouter_video 内部忽略的异常", exc_info=True)
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
                    logger.debug("_request_openrouter_video 内部忽略的异常", exc_info=True)
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
                        # unsigned_urls 可能是空列表 []（任务完成但 URL 列表为空）。
                        # 用 next(iter(...), None) 安全取首个元素，避免 IndexError。
                        unsigned = data.get("unsigned_urls")
                        video_url = data.get("content") or (
                            next(iter(unsigned), None)
                            if isinstance(unsigned, list) and unsigned
                            else None
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


