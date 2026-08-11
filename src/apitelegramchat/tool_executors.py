# tool_executors.py
import asyncio
import os
import subprocess
import shlex
import random
import aiohttp
import json
import hashlib
import time
from pathlib import Path
from apitelegramchat.workspace_paths import workspace_root, workspace_workdir, runtime_cache_root
import re
import html
import logging
from typing import Optional, List
from urllib.parse import urlparse
from apitelegramchat.workspace_utils import (
    _get_workspace_lock,
    _sync_workspace_from_r2,
    _sync_workspace_to_r2,
    _async_sync_workspace_to_r2,  # 新增导入
)

from apitelegramchat.sandbox import (
    build_sandbox_argv, build_sandbox_env,
    watchdog, _preexec_sandbox,
    SANDBOX_TIMEOUT_SEC,
)

from apitelegramchat.config import (
    R2_PUBLIC_URL,
    MAX_CONCURRENT_TOOLS,
    BASE_URL,
    GEOAPIFY_KEY,
    TOMTOM_API_KEY,
    ORS_API_KEY,
)

_TOOL_TIMEOUT_MARKER = "__TOOL_TIMEOUT__"

import shutil
from apitelegramchat.s3_utils import upload_bytes_to_r2, file_exists_in_r2, download_from_r2, list_r2_objects, delete_r2_object
from apitelegramchat.search_engine import (
    execute_web_search,
    execute_fetch_url,
    execute_wikipedia,
    execute_exchange_rate,
    execute_hacker_news,
    execute_book_lookup,
    execute_weather,
    execute_news,
    execute_crypto_price,
    execute_ip_geo,
    execute_qr_code,
    execute_done,
    execute_generate_image,
    execute_generate_video,
    execute_image_search,
    # 地图工具
    execute_geocode,
    execute_search_poi,
    execute_route,
    execute_distance,
    execute_place_details,
    execute_elevation,
    execute_traffic,
    execute_isochrone,
    execute_text_editor,
    # 任务工具
    execute_todo,
    # 长期记忆工具
    execute_memory,
    # 技能注册表
    # 子 agent 工具
    execute_subagent,
)
from apitelegramchat.skills import (
    catalog_text as skill_catalog_text,
    read_skill_text as skill_read_text,
    activate_skill as skill_activate_skill,
    sync_skill_assets_to_workspace as _skill_sync_assets,
    SKILL_ASSETS_DIRNAME,
)
from apitelegramchat.todo_tool import (
    render_todo_card,
)
from apitelegramchat.memory_tool import render_memory_card
try:
    from apitelegramchat.subagent_tool import render_subagent_card
except Exception:  # pragma: no cover - optional dependency fallback
    def render_subagent_card(*args, **kwargs):
        return "<b>Subagent</b>"
from apitelegramchat.utils import escape_html

logger = logging.getLogger(__name__)

# ---------- 信号量控制并发工具调用 ----------
tool_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

MAX_TOOL_RESPONSE_LEN = 16000

def _truncate_tool_result(result: str) -> str:
    if len(result) > MAX_TOOL_RESPONSE_LEN:
        return result[:MAX_TOOL_RESPONSE_LEN] + "\n…[内容过长已截断]"
    return result

def extract_domain(url: str) -> str:
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split('/')[0]

# =====================================================================
# Persistent runtime state
# =====================================================================

_RUNTIME_STATE_FILENAME = ".runtime_cache/runtime.json"


def _runtime_state_path(chat_id: int) -> Path:
    return workspace_root(chat_id) / _RUNTIME_STATE_FILENAME


def _tool_version(exe: str) -> str | None:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (proc.stdout or "").splitlines()
    return line[0].strip()[:300] if line else None


def _prepare_runtime_once(chat_id: int, cache_root: Path) -> dict:
    """Record host toolchain discovery once per persistent workspace.

    This deliberately does *not* install compilers per Bash invocation. The base image
    owns the toolchain; the workspace owns only reusable caches and a small manifest.
    """
    state_path = _runtime_state_path(chat_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("schema") == 1:
                return state
        except (OSError, ValueError, TypeError):
            pass

    tools = {
        name: {
            "path": shutil.which(name),
            "version": _tool_version(name),
        }
        for name in ("python3", "gcc", "g++", "clang", "clang++", "make", "cmake", "ccache")
    }
    state = {
        "schema": 1,
        "prepared": True,
        "cache_root": str(cache_root),
        "tools": tools,
    }
    try:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to persist runtime state chat_id=%s: %s", chat_id, exc)
    return state


# =====================================================================
# BashSession —— 每会话独立沙箱
# =====================================================================
class BashSession:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._started = False
        self.workspace = workspace_root(chat_id)
        self.workdir = workspace_workdir(chat_id)
        self._watchdog_task: Optional[asyncio.Task] = None
        self._runtime_state: Optional[dict] = None
        self._runtime_prepare_lock = asyncio.Lock()
        # active skill 只作为上下文状态保留，不再改变 Bash 的 cwd。
        # cwd 必须由模型通过 `cd` 自己控制，这样 persistent bash 才真正保持
        # 环境变量、当前目录和 shell 状态，且不会因为 skill 匹配而把工作区
        # 根目录意外切到 `.skills/<skill_id>`。
        self._active_skill_id: Optional[str] = None

    def set_active_skill(self, skill_id: Optional[str]) -> None:
        """记录当前 skill 上下文；不修改 Bash 当前工作目录。"""
        self._active_skill_id = skill_id or None

    async def start(self):
        """启动 bash 进程，套上 Landlock 沙箱 + rlimit + no-new-privs"""
        if self.proc is not None and self.proc.returncode is None:
            return

        # workspace 目录权限 700，防跨 chat 读取
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chmod(self.workdir, 0o700)

        argv = build_sandbox_argv()
        env = build_sandbox_env(self.workspace, self.chat_id)
        cache_root = runtime_cache_root(self.chat_id)
        async with self._runtime_prepare_lock:
            if self._runtime_state is None:
                # One-time discovery per persistent workspace. Subsequent Bash restarts
                # reuse the manifest instead of "preparing" the toolchain again.
                self._runtime_state = await asyncio.to_thread(
                    _prepare_runtime_once, self.chat_id, cache_root
                )

        # ★ Landlock：把文件系统访问限制在 workspace 目录内，
        #   拒绝访问 /tmp 其他子目录（state/、r2_cache/）、/app 源码等。
        #   通过 functools.partial 把 workspace 路径传给 preexec。
        import functools
        preexec = functools.partial(
            _preexec_sandbox,
            str(self.workdir.absolute()),
        )

        logger.info(
            "Starting bash session chat_id=%s runtime_prepared=%s cache=%s",
            self.chat_id,
            bool(self._runtime_state and self._runtime_state.get("prepared")),
            cache_root,
        )

        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,  # ★ 关键: 不传任何敏感变量
            cwd=str(self.workdir.absolute()),  # ★ 关键: 沙箱进程启动即位于 workspace
            start_new_session=True,  # ★ 关键: 创建新会话，便于 killpg
            preexec_fn=preexec,  # Landlock + no-new-privs + rlimit
        )

        # 启动看门狗（fork bomb 防护）
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                watchdog(self.proc), name=f"watchdog-{self.chat_id}"
            )

        self._started = True

    # ===================== 命令安全检查（最小黑名单） =====================
    # 设计原则: 不限制语法（heredoc/管道/重定向/&&/|| 全部允许），
    #          只拦截极端灾难模式，剩余靠沙箱兜底
    _DANGEROUS_PATTERNS = [
        # rm -rf / 或 rm -rf /*
        (re.compile(r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)\s+/(?:\s|$|\*)'),
         "rm -rf /"),
        # fork bomb
        (re.compile(r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:'),
         "fork bomb"),
        # 写裸设备
        (re.compile(r'\bdd\s+if=\S+\s+of=/dev/(?!null|zero|random|urandom)'),
         "dd to raw device"),
        # mkfs 任意设备
        (re.compile(r'\bmkfs\.\w+\s+/dev/'),
         "mkfs on device"),
        # 写 /dev/mem /dev/kmem
        (re.compile(r'\bof=/dev/(mem|kmem|port)'),
         "write to kernel memory"),
        # :(){...} 的变体
        (re.compile(r'\.\s*\(\s*\)\s*\{'),
         "anonymous fork function"),
    ]

    def _is_safe(self, command: str) -> bool:
        """最小黑名单，仅拦极端操作；其余靠沙箱"""
        if not command or not command.strip():
            return False
        for pattern, name in self._DANGEROUS_PATTERNS:
            if pattern.search(command):
                logger.warning(f"🚫 Bash rejected ({name}) chat_id={self.chat_id}: {command[:200]}")
                return False
        return True

    # ===================== 执行命令 =====================
    async def execute(self, command: str, timeout: int = SANDBOX_TIMEOUT_SEC, progress_callback=None) -> str:
        """在沙箱中执行 bash 命令，超时自动终止"""
        lock = await _get_workspace_lock(self.chat_id)
        async with lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()

            # 从 R2 拉取最新文件（保留原行为）
            try:
                await _sync_workspace_from_r2(self.chat_id)
            except Exception as e:
                logger.warning(f"_sync_from_r2 failed (continue): {e}")

            if not self._is_safe(command):
                return f"Error: Command rejected for security reasons: {command}"

            marker = f"__END_{random.randint(100000, 999999)}__"
            cwd_marker = f"__CWD_{random.randint(100000, 999999)}__"
            # 不要在每次调用前强制 `cd`：模型可以在 persistent bash 中自行
            # `cd .skills/<skill_id>`，后续命令会继续留在该目录。默认 shell
            # 启动目录仍然是 workspace 根目录。
            # ★ 关键：在 echo marker 前先输出一个换行，确保 marker 单独占一行。
            #   如果命令输出不以换行结尾（如 cat 无换行文件、printf 无 \n），
            #   echo 的输出会粘在前一行，readline() 永远读不到以 marker 开头的行，
            #   导致整个会话 hang 死。
            # 同时记录命令结束后的真实 PWD，用于结果显示；不会改变 shell 状态。
            full_cmd = (
                f"{command}; echo; printf '{cwd_marker} %s\n' \"$PWD\"; "
                f"echo '{marker} $?'\n"
            )

            try:
                self.proc.stdin.write(full_cmd.encode('utf-8'))
                await self.proc.stdin.drain()

                output_parts = []
                exit_code = "unknown"
                progress_last_emit = 0.0
                progress_chars_at_emit = 0
                progress_min_interval = 0.20
                progress_min_chars = 256

                async def emit_progress(force: bool = False):
                    nonlocal progress_last_emit, progress_chars_at_emit
                    if progress_callback is None or not output_parts:
                        return
                    output_text = "".join(output_parts)
                    now = time.monotonic()
                    grew = len(output_text) - progress_chars_at_emit
                    if not force and grew < progress_min_chars and (now - progress_last_emit) < progress_min_interval:
                        return
                    # 前端草稿只需要最近一段日志；完整输出仍由最终结果保留。
                    preview_text = output_text[-8000:]
                    try:
                        result = progress_callback(preview_text)
                        if asyncio.iscoroutine(result):
                            await result
                        progress_last_emit = now
                        progress_chars_at_emit = len(output_text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as cb_error:
                        # UI 推送失败绝不能影响 Bash 本身执行。
                        logger.debug(f"bash progress callback failed: {cb_error}")

                async def read_until_marker():
                    nonlocal exit_code
                    # marker 可能跨 chunk 被拆开，因此只保留一个很小的尾部用于跨 chunk 匹配；
                    # 已经确定不可能包含 marker 的前缀立即写入 output_parts，避免每次都 O(n) 拼接。
                    pending = ""
                    keep_tail = len(marker) + 64
                    while True:
                        chunk = await self.proc.stdout.read(4096)
                        if not chunk:
                            if pending:
                                output_parts.append(pending)
                                pending = ""
                            break

                        pending += chunk.decode('utf-8', errors='replace')
                        marker_pos = pending.find(marker)
                        if marker_pos >= 0:
                            # marker 前是命令真实输出；后面紧接着是 echo 的退出码。
                            if marker_pos:
                                output_parts.append(pending[:marker_pos])
                            marker_tail = pending[marker_pos:]
                            match = re.search(rf"{re.escape(marker)}\s+(-?\d+)", marker_tail)
                            if match:
                                exit_code = match.group(1)
                            pending = ""
                            await emit_progress(force=True)
                            break

                        if len(pending) > keep_tail:
                            output_parts.append(pending[:-keep_tail])
                            pending = pending[-keep_tail:]

                        await emit_progress(force=False)

                await asyncio.wait_for(read_until_marker(), timeout=timeout)

                output = "".join(output_parts)
                if len(output) > 20000:
                    output = output[:20000] + "\n... (truncated)"
                output = re.sub(r'\x1b\[[0-9;]*m', '', output)

                # 提取命令结束后的真实 PWD，同时把内部 marker 从用户输出中移除。
                actual_cwd = str(self.workdir.absolute())
                cwd_match = re.search(rf'(?m)^' + re.escape(cwd_marker) + r' (.+)$', output)
                if cwd_match:
                    actual_cwd = cwd_match.group(1).strip()
                    output = re.sub(rf'(?m)^' + re.escape(cwd_marker) + r' .*$\n?', '', output)

                # 同步回 R2（异步，不阻塞返回）
                asyncio.create_task(_async_sync_workspace_to_r2(self.chat_id))

                return (f"Command: {command}\n"
                        f"Cwd: {actual_cwd}\n"
                        f"Exit code: {exit_code}\n"
                        f"Sandbox: landlock\n"
                        f"Output:\n{output}")

            except asyncio.TimeoutError:
                logger.warning(f"Bash timeout chat_id={self.chat_id} cmd={command[:100]}")
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass
                # 重启会话
                await self.close()
                return f"Error: Command timed out after {timeout} seconds (sandbox killed & session will restart)"

            except Exception as e:
                logger.exception(f"Bash execute error chat_id={self.chat_id}")
                return f"Error: {str(e)}"

    # ===================== 关闭会话 =====================
    async def close(self):
        # 取消看门狗
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("bash session force-close wait timed out chat_id=%s", self.chat_id)
            except Exception:
                logger.exception("bash session close wait failed chat_id=%s", self.chat_id)
            finally:
                self.proc = None
                self._started = False
            return
        self.proc = None
        self._started = False

# =====================================================================
# BashSessionManager —— 多 chat 共享管理
# =====================================================================
class BashSessionManager:
    def __init__(self):
        self._sessions: dict = {}
        self._lock = asyncio.Lock()

    async def get_session(self, chat_id: int) -> BashSession:
        async with self._lock:
            if chat_id not in self._sessions:
                session = BashSession(chat_id)
                await session.start()
                self._sessions[chat_id] = session
            else:
                # 进程已死则重建
                s = self._sessions[chat_id]
                if s.proc is None or s.proc.returncode is not None:
                    await s.start()
            return self._sessions[chat_id]

    async def restart_session(self, chat_id: int) -> str:
        async with self._lock:
            session = self._sessions.get(chat_id)
            if session:
                await session.close()
                del self._sessions[chat_id]
            new_session = BashSession(chat_id)
            await new_session.start()
            self._sessions[chat_id] = new_session
            return f"Bash session restarted (sandbox=landlock)"

    async def cleanup_all(self):
        """优雅关闭所有会话（应用退出时调用）"""
        async with self._lock:
            for s in self._sessions.values():
                try:
                    await s.close()
                except Exception:
                    pass
            self._sessions.clear()

_bash_manager = BashSessionManager()

# =====================================================================
# execute_bash —— 工具调用入口（保持原签名，外部无需修改）
# =====================================================================
async def execute_bash(chat_id: int, command: str = "", restart: bool = False, skill_id: Optional[str] = None, progress_callback=None) -> str:
    if restart:
        result = await _bash_manager.restart_session(chat_id)
        # 重启后也异步同步一次
        asyncio.create_task(_async_sync_workspace_to_r2(chat_id))
        return result
    if not command:
        return "Error: command is required (or set restart=true)"
    try:
        session = await _bash_manager.get_session(chat_id)
    except RuntimeError as e:
        return f"Error: {e}"
    # active skill 作为上下文同步，但不改变 persistent bash 的 cwd。
    # cwd 由模型在需要使用 skill 时自行 `cd .skills/<skill_id>` 控制。
    session.set_active_skill(skill_id)
    # 执行命令，内部已包含异步同步
    return await session.execute(command, progress_callback=progress_callback)

# ---------- 静态地图生成 ----------
async def _get_static_map_image(
        lat: float,
        lon: float,
        markers: list = None,
        zoom: int = 15,
        width: int = 600,
        height: int = 400
) -> Optional[str]:
    cache_key = hashlib.md5(f"{lat}{lon}{zoom}{markers}".encode()).hexdigest()
    r2_key = f"maps/{cache_key}.png"

    if await file_exists_in_r2(r2_key):
        if R2_PUBLIC_URL:
            return f"{R2_PUBLIC_URL.rstrip('/')}/{r2_key}"

    # === [amap_integration patch] 高德静态地图优先 ===
    try:
        from apitelegramchat import amap_integration as _amap
        if _amap.is_enabled():
            amap_url = _amap.static_map_url_amap(
                lat, lon,
                markers=[{'lat': m['lat'], 'lon': m['lon']} for m in markers] if markers else None,
                zoom=zoom, width=width, height=height,
            )
            if amap_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            amap_url,
                            timeout=aiohttp.ClientTimeout(total=12)
                        ) as resp:
                            if resp.status == 200:
                                img_bytes = await resp.read()
                                if len(img_bytes) > 500 and img_bytes[:1] not in (b'{', b'['):
                                    uploaded_url = await upload_bytes_to_r2(
                                        img_bytes, r2_key, 'image/png'
                                    )
                                    return uploaded_url
                except Exception as e:
                    logger.warning(f'高德静态地图失败: {e}')
    except Exception:
        pass
    # === [/amap_integration patch] ===
    # 备用来源列表
    marker_str = ""
    if markers:
        colors = ['blue', 'green', 'orange', 'purple', 'brown', 'red']
        for idx, m in enumerate(markers[:10]):
            color = colors[idx % len(colors)]
            label = chr(65 + idx)
            marker_str += f"&markers={color}%7C{m['lat']},{m['lon']}"
    else:
        marker_str = f"&markers=red%7C{lat},{lon}"

    geoapify_key = GEOAPIFY_KEY or ""
    if geoapify_key:
        url = (
            f"https://maps.geoapify.com/v1/staticmap"
            f"?style=osm-carto&width={width}&height={height}"
            f"&center=lonlat:{lon},{lat}&zoom={zoom}"
            f"&apiKey={geoapify_key}"
        )
        if markers:
            for idx, m in enumerate(markers[:10]):
                label = chr(65 + idx)
                url += f"&marker=lonlat:{m['lon']},{m['lat']};color:%23ff3300;size:medium;text:{label}"
        else:
            url += f"&marker=lonlat:{lon},{lat};color:%23ff3300;size:medium"
        sources = [url]
    else:
        sources = [
            (
                f"https://staticmap.openstreetmap.de/staticmap.php"
                f"?center={lat},{lon}&zoom={zoom}&size={width}x{height}{marker_str}"
            )
        ]

    for static_url in sources:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        static_url,
                        timeout=aiohttp.ClientTimeout(total=12)
                ) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        if len(img_bytes) > 500:  # 排除空响应
                            uploaded_url = await upload_bytes_to_r2(
                                img_bytes, r2_key, "image/png"
                            )
                            return uploaded_url
        except Exception as e:
            # 仅记录主机名和异常，避免泄露包含 apiKey 的完整 URL
            try:
                from urllib.parse import urlparse as _up
                _host = _up(static_url).netloc or "unknown"
            except Exception:
                _host = "unknown"
            logger.warning(f"静态地图源失败 host={_host}: {e}")
            continue

    return None

# ---------- 工具结果格式化 ----------

# Magic marker emitted by ai_handlers.run_one on asyncio.TimeoutError.
# format_tool_result intercepts this BEFORE any other branch so we can
# surface a user-safe message and avoid leaking the actual timeout value.
# Human-readable label per tool name, used when surfacing timeout messages.
# Falls back to the raw fn_name if not listed here.
_TOOL_TIMEOUT_LABELS = {
    "web_search": "Web search",
    "fetch_url": "Page fetch",
    "wikipedia": "Wikipedia lookup",
    "exchange_rate": "Exchange rate lookup",
    "hacker_news": "Hacker News fetch",
    "book_lookup": "Book lookup",
    "weather": "Weather fetch",
    "news": "News fetch",
    "crypto_price": "Crypto price lookup",
    "ip_geo": "IP geolocation",
    "qr_code": "QR code generation",
    "generate_video": "Video generation",
    "image_search": "Image search",
    "geocode": "Geocoding",
    "search_poi": "POI search",
    "route": "Route planning",
    "distance": "Distance calculation",
    "place_details": "Place details fetch",
    "elevation": "Elevation lookup",
    "traffic": "Traffic lookup",
    "isochrone": "Isochrone calculation",
    "text_editor": "Editor operation",
    "bash": "Bash command",
    "present_files": "File presentation",
}

async def format_tool_result(fn_name: str, fn_args: dict, result_str: str) -> tuple[str, str]:
    def escape_text(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ---- Intercept timeout magic marker BEFORE any other branch ----
    # The raw exception (with TOOL_CALL_TIMEOUT seconds) is kept in
    # logger.error on the backend; the UI only sees the friendly version.
    if result_str == _TOOL_TIMEOUT_MARKER:
        label = _TOOL_TIMEOUT_LABELS.get(fn_name, fn_name)
        summary = f"⏱️ {label} timed out"
        details_html = "Execution exceeded the timeout limit. Please refine your request or try again later."
        return summary, details_html

    if fn_name == "web_search":
        query = fn_args.get('query', '')
        # execute_web_search 返回固定 envelope：成功数/请求数；只在旧格式下
        # 才回退到标题计数，避免把失败误报成 0 results。
        count_match = re.search(r'\[成功:[^\]]+\].*?[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]', result_str or "", re.S)
        if count_match:
            num_results = int(count_match.group(1))
        else:
            num_results = result_str.count("标题：") if "标题：" in result_str else 0
        if result_str.lstrip().startswith("❌"):
            summary = "Search failed"
        elif query and num_results == 1:
            summary = f"{query} 1 result"
        elif query:
            summary = f"{query} {num_results} results"
        else:
            summary = "Searched the web"
        if "标题：" in result_str and "链接：" in result_str:
            items_html = ""
            current_title = ""
            current_link = ""
            for line in result_str.split('\n'):
                if "标题：" in line:
                    current_title = line.split("标题：")[-1].strip()
                elif "链接：" in line:
                    current_link = line.split("链接：")[-1].strip()
                    if current_title and current_link:
                        if current_link.startswith("http"):
                            domain = current_link.split('/')[2] if '//' in current_link else current_link
                            items_html += f"<li><a href=\"{current_link}\">{current_title}</a> <code>{domain}</code></li>"
                        else:
                            items_html += f"<li>{current_title} <code>{current_link}</code></li>"
                        current_title = ""
                        current_link = ""
            if items_html:
                details_html = f"<ol>{items_html}</ol>"
            else:
                details_html = escape_text(result_str[:60000])
        else:
            details_html = escape_text(result_str[:60000])
        return summary, details_html

    elif fn_name == "fetch_url":
        url = fn_args.get('url', '')
        domain = extract_domain(url)
        if "失败" in result_str or "超时" in result_str or "Failed" in result_str or "Error" in result_str:
            logger.error(f"[fetch_url] Failed to fetch {url}: {result_str[:500]}")
            summary = f"🌐 Failed to fetch {domain}"
            details_html = "Unable to retrieve content. Check the URL or try again later."
        else:
            title = domain
            if "🏷️" in result_str:
                match = re.search(r'🏷️\s+([^\n]+)', result_str)
                if match:
                    title = match.group(1).strip()
            summary = f"🌐 Fetched: {title}"
            safe_url = html.escape(url)
            safe_domain = html.escape(domain)
            safe_title = html.escape(title)
            details_html = f"{safe_title} <a href=\"{safe_url}\">{safe_domain}</a>"
        return summary, details_html

    elif fn_name == "weather":
        try:
            weather_data = json.loads(result_str)
            if "error" in weather_data:
                error_msg = weather_data["error"]
                summary = "🌤️ 天气查询失败"
                details_html = f"<pre><code>{error_msg}</code></pre>"
                return summary, details_html

            city = weather_data.get("city", "未知")
            current = weather_data.get("current", {})
            hourly = weather_data.get("hourly", [])
            daily = weather_data.get("daily", [])
            unit_display = "℃" if weather_data.get("unit") == "C" else "℉"

            temp = current.get("temp", "N/A")
            cond = current.get("condition", "")
            summary = f"🌤️ {city} {temp}{unit_display} {cond}"

            details_html = f"<b>{city} 详细天气</b><br/><br/>"
            details_html += "<h3>📍 当前天气</h3>"
            details_html += f"🌡️ 温度：{temp}{unit_display}（体感 {current.get('feels_like', 'N/A')}{unit_display}）<br/>"
            details_html += f"💧 湿度：{current.get('humidity', 'N/A')}% 💨 风速：{current.get('wind', 'N/A')} km/h"
            if current.get('wind_gust', 'N/A') != 'N/A':
                details_html += f"（阵风 {current['wind_gust']} km/h）"
            details_html += "<br/>"
            details_html += f"☁️ 云量：{current.get('cloudcover', 'N/A')}% 🌡️ 气压：{current.get('pressure', 'N/A')} mb<br/>"
            details_html += f"👁️ 能见度：{current.get('visibility', 'N/A')} km ☀️ 紫外线指数：{current.get('uvIndex', 'N/A')}<br/>"
            details_html += f"🌧️ 降水：{current.get('precip', '0.0')} mm 🧭 风向：{current.get('wind_dir', 'N/A')} ({current.get('wind_deg', 'N/A')}°)<br/>"
            details_html += f"🕒 观测时间：{current.get('obs_time', '')}<br/>"
            details_html += f"🌥️ 天气状况：{cond}<br/><br/>"

            if daily:
                details_html += "<details><summary>📅 未来几天预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>日期</th><th>天气</th><th>最高</th><th>最低</th><th>UV</th><th>日出</th><th>日落</th><th>降水%</th></tr>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    cond_d = day.get("condition", "")
                    max_t = day.get("max", "N/A")
                    min_t = day.get("min", "N/A")
                    max_display = f"{max_t}{unit_display}" if max_t != "N/A" else "--"
                    min_display = f"{min_t}{unit_display}" if min_t != "N/A" else "--"
                    uv = day.get("uvIndex", "N/A")
                    sunrise = day.get("sunrise", "--")
                    sunset = day.get("sunset", "--")
                    rain = day.get("chance_of_rain", "0") + "%"
                    details_html += f"<tr><td>{date}</td><td>{cond_d}</td><td align='right'>{max_display}</td><td align='right'>{min_display}</td><td align='center'>{uv}</td><td>{sunrise}</td><td>{sunset}</td><td align='right'>{rain}</td></tr>"
                details_html += "</table><br/>"
                details_html += "<details><summary>🌙 天文 &amp; 其他概率</summary><br/>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    moon_phase = day.get("moon_phase", "--")
                    moon_illum = day.get("moon_illumination", "0") + "%"
                    snow = day.get("chance_of_snow", "0") + "%"
                    thunder = day.get("chance_of_thunder", "0") + "%"
                    fog = day.get("chance_of_fog", "0") + "%"
                    frost = day.get("chance_of_frost", "0") + "%"
                    details_html += f"<b>{date}</b>：月相 {moon_phase}（{moon_illum}），雪 {snow}，雷暴 {thunder}，雾 {fog}，霜冻 {frost}<br/>"
                details_html += "</details><br/>"
                details_html += "</details><br/>"

            if hourly:
                details_html += "<details><summary>⏰ 逐时预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>时间</th><th>天气</th><th>温度</th><th>降水</th><th>湿度</th><th>风速</th><th>气压</th><th>UV</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    cond_h = h.get("condition", "")
                    temp_h = h.get("temp", "N/A")
                    precip_h = h.get("precip", "0")
                    humidity_h = h.get("humidity", "N/A")
                    wind_speed_h = h.get("wind_speed", "N/A")
                    pressure_h = h.get("pressure", "N/A")
                    uv_h = h.get("uvIndex", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td>{cond_h}</td><td align='right'>{temp_h}{unit_display}</td><td align='right'>{precip_h} mm</td><td align='right'>{humidity_h}%</td><td align='right'>{wind_speed_h} km/h</td><td align='right'>{pressure_h} mb</td><td align='center'>{uv_h}</td></tr>"
                details_html += "</table><br/>"
                details_html += "<details><summary>🌪️ 逐时额外数据（阵风、云量、能见度、风向、概率、露点等）</summary><br/>"
                details_html += "<table bordered striped cellpadding='2'>"
                details_html += "<tr><th>时间</th><th>阵风</th><th>云量</th><th>能见度</th><th>风向</th><th>雨%</th><th>雪%</th><th>雷暴%</th><th>雾%</th><th>霜冻%</th><th>露点</th><th>热指数</th><th>风寒</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    gust = h.get("wind_gust", "N/A")
                    cloud = h.get("cloudcover", "N/A")
                    vis = h.get("visibility", "N/A")
                    wind_dir = h.get("wind_dir", "N/A")
                    rain = h.get("chance_of_rain", "0") + "%"
                    snow = h.get("chance_of_snow", "0") + "%"
                    thunder = h.get("chance_of_thunder", "0") + "%"
                    fog = h.get("chance_of_fog", "0") + "%"
                    frost = h.get("chance_of_frost", "0") + "%"
                    dew = h.get("DewPointC", "N/A")
                    heat = h.get("HeatIndexC", "N/A")
                    chill = h.get("WindChillC", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td align='right'>{gust} km/h</td><td align='right'>{cloud}%</td><td align='right'>{vis} km</td><td>{wind_dir}</td><td align='right'>{rain}</td><td align='right'>{snow}</td><td align='right'>{thunder}</td><td align='right'>{fog}</td><td align='right'>{frost}</td><td align='right'>{dew}°C</td><td align='right'>{heat}°C</td><td align='right'>{chill}°C</td></tr>"
                details_html += "</table>"
                details_html += "</details><br/>"
                details_html += "</details><br/>"

            tips = []
            cond_lower = cond.lower()
            if "雨" in cond or "rain" in cond_lower:
                tips.append("🌂 今天有降水，出门记得带伞。")
            if "霾" in cond or "haze" in cond_lower or "烟雾" in cond:
                tips.append("😷 空气中有雾霾，建议佩戴口罩或减少户外活动。")
            try:
                if int(temp) > 30:
                    tips.append("☀️ 气温较高，注意防暑降温，多补充水分。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('uvIndex', 0)) >= 8:
                    tips.append("🧴 紫外线指数高，外出请做好防晒。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('visibility', 10)) < 2:
                    tips.append("🌫️ 能见度较低，驾车请减速慢行。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('wind', 0)) > 30:
                    tips.append("💨 风速较大，注意防风。")
            except (ValueError, TypeError):
                pass
            if "雪" in cond or "snow" in cond_lower:
                tips.append("❄️ 有降雪，路面湿滑，注意出行安全。")
            if tips:
                details_html += "<b>💡 温馨提示</b><br/>" + "<br/>".join(tips)

            return summary, details_html

        except json.JSONDecodeError:
            safe_log = escape_text(result_str[:60000])
            summary = "🌤️ 天气数据"
            details_html = f"<pre><code>{safe_log}</code></pre>"
            return summary, details_html

    elif fn_name == "wikipedia":
        query = fn_args.get('query', '')
        lang = fn_args.get('lang', 'zh')
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        wiki_url = f"https://{lang}.wikipedia.org/wiki/{encoded_query}"
        summary = f"📚 {query}"
        details_html = f'<a href="{wiki_url}">{query}</a>'
        return summary, details_html

    elif fn_name == "exchange_rate":
        base = fn_args.get('base', 'USD')
        summary = f"💱 {base} 汇率"
        details_html = result_str
        return summary, details_html

    elif fn_name == "hacker_news":
        summary = "📰 Hacker News"
        details_html = result_str
        return summary, details_html

    elif fn_name == "book_lookup":
        query = fn_args.get('query', '')
        summary = f"📖 {query}"
        details_html = result_str
        return summary, details_html

    elif fn_name == "news":
        source = fn_args.get('source', 'news')
        summary = f"📰 {source.upper()} 新闻"
        details_html = result_str
        return summary, details_html

    elif fn_name == "crypto_price":
        coin = fn_args.get('coin', '')
        summary = f"💰 {coin.upper()} 价格"
        details_html = result_str
        return summary, details_html

    elif fn_name == "ip_geo":
        ip = fn_args.get('ip', '')
        summary = f"🌍 IP 地理位置" + (f" {ip}" if ip else "")
        details_html = result_str
        return summary, details_html

    elif fn_name == "qr_code":
        if "✅ 二维码生成成功" in result_str:
            img_match = re.search(r'图片链接：([^\s]+)', result_str)
            content_match = re.search(r'内容：([^\n]+)', result_str)
            if img_match:
                img_url = img_match.group(1)
                content_text = content_match.group(1) if content_match else "已编码内容"
                summary = "📱 二维码已生成"
                # 转义 URL（R2 presigned URL 含大量 & 需转义为 &amp;）
                safe_url = escape_html(img_url)
                details_html = (
                    f'<img src="{safe_url}"/><br/>'
                    f'<b>✅ 二维码生成成功</b><br/>'
                    f'<b>内容：</b>{escape_text(content_text)}<br/>'
                    f'<b>链接：</b><a href="{safe_url}">📷 点击查看 / 下载二维码</a>'
                )
                return summary, details_html
        summary = "📱 二维码"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "generate_image_from_text":
        if "✅" in result_str:
            lines = result_str.splitlines()
            urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
            if urls:
                count = len(urls)
                summary = f"🎨 Generated {count} image" + ("" if count == 1 else "s")
                img_tags = "".join(f'<img src="{escape_html(u)}"/>' for u in urls)
                # 用简短的"图片 1 / 图片 2"文本链接替代裸 URL，避免长 R2 presigned URL 刷屏
                link_items = "".join(
                    f'<li><a href="{escape_html(u)}">图片 {i + 1}</a></li>'
                    for i, u in enumerate(urls)
                )
                caption = f"已生成 {count} 张图片：<ul>{link_items}</ul>"
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if count == 1:
                    details_html = f'<figure>{img_tags}<figcaption>{caption}</figcaption></figure>'
                else:
                    details_html = f'<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>'
                return summary, details_html
        summary = "🎨 Image generation"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "edit_image_with_reference":
        if "✅" in result_str:
            lines = result_str.splitlines()
            urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
            if urls:
                count = len(urls)
                summary = f"🎨 Edited {count} image" + ("" if count == 1 else "s")
                img_tags = "".join(f'<img src="{escape_html(u)}"/>' for u in urls)
                link_items = "".join(
                    f'<li><a href="{escape_html(u)}">图片 {i + 1}</a></li>'
                    for i, u in enumerate(urls)
                )
                caption = f"已编辑 {count} 张图片：<ul>{link_items}</ul>"
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if count == 1:
                    details_html = f'<figure>{img_tags}<figcaption>{caption}</figcaption></figure>'
                else:
                    details_html = f'<tg-slideshow>{img_tags}<figcaption>{caption}</figcaption></tg-slideshow>'
                return summary, details_html
        summary = "🎨 Image editing"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "generate_video":
        # 视频通过 <figure><video> 内嵌在工具结果卡片里渲染（Telegram Rich Message
        # 支持视频 block 与文本同消息共存，参见 Rich Message Formatting Options）。
        # execute_generate_video 返回的结构：
        #   ✅ 已生成视频。
        #   视频链接：https://...
        if "✅" in result_str:
            url_match = re.search(r'视频链接：(https?://[^\s]+)', result_str)
            if url_match:
                # ⚠️ R2 presigned URL 含大量 & 查询参数（X-Amz-Algorithm、X-Amz-Credential、
                # X-Amz-Signature 等），HTML 属性值中未转义的 & 会被 Telegram HTML
                # 解析器当作实体名起点，导致 URL 被截断 → RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
                # 必须用 escape_html 转义（与 _agentic_loop_native_video 老路径一致）。
                video_url = url_match.group(1).strip()
                duration_str = ""
                m = re.search(r'(\d+)\s*秒', fn_args.get("prompt", "") or "")
                if m:
                    duration_str = f" · {m.group(1)}s"
                summary = f"🎬 Video generated{duration_str}"
                # <figure><video> 是一个独立 media block，可以与其他 block 同消息发送；
                # 附带简短文本链接 caption，避免裸 R2 presigned URL 刷屏
                details_html = (
                    f'<figure><video src="{escape_html(video_url)}"></video>'
                    f'<figcaption><a href="{escape_html(video_url)}">下载 / 查看视频</a></figcaption>'
                    f'</figure>'
                )
                return summary, details_html
        summary = "🎬 Video generation"
        details_html = escape_text(result_str)
        return summary, details_html

    elif fn_name == "image_search":
        if "✅" in result_str:
            lines = result_str.splitlines()
            urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
            if urls:
                summary = f"🖼️ 找到 {len(urls)} 张图片"
                img_tags = "".join(f'<img src="{html.escape(u)}"/>' for u in urls)
                # 用简短的"图片 1 / 图片 2"文本链接替代裸 URL，避免长 R2 presigned URL 刷屏
                link_items = ""
                for i, u in enumerate(urls):
                    link_items += f'<li><a href="{html.escape(u)}">图片 {i + 1}</a></li>'
                link_list = f"<ul>{link_items}</ul>" if link_items else ""
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if len(urls) == 1:
                    media_html = f'<figure>{img_tags}<figcaption>点击图片查看大图</figcaption></figure>'
                else:
                    media_html = f'<tg-slideshow>{img_tags}<figcaption>点击图片查看大图</figcaption></tg-slideshow>'
                details_html = (
                    f'{media_html}'
                    f'<br/>{link_list}'
                )
                return summary, details_html
        summary = "🖼️ 图片搜索"
        details_html = escape_text(result_str)
        return summary, details_html

    # ===================== 地图工具 =====================
    elif fn_name == "geocode":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                lat, lon = data["lat"], data["lon"]
                name = data.get("display_name", "未知地址")
                road = data.get("road", "")
                city = data.get("city", "")
                county = data.get("county", "")
                state = data.get("state", "")
                country = data.get("country", "")
                postcode = data.get("postcode", "")
                gcj_lat = data.get("gcj02_lat")
                gcj_lon = data.get("gcj02_lon")
                coord_system = data.get("coord_system", "WGS-84")
                summary = f"📍 {lat:.4f}, {lon:.4f}"
                map_img_url = await _get_static_map_image(lat, lon, zoom=15)
                map_html = f'<img src="{map_img_url}"/>' if map_img_url else f'<tg-map lat="{lat}" long="{lon}" zoom="15"/>'
                gcj_html = ""
                if gcj_lat is not None and gcj_lon is not None:
                    gcj_html = f"<b>🧭 高德坐标（GCJ-02）：</b>{gcj_lat:.6f}, {gcj_lon:.6f}<br/>"
                details_html = f"""
{map_html}
<b>📍 坐标（{escape_text(coord_system)}）：</b>{lat:.6f}, {lon:.6f}<br/>
{gcj_html}
<b>📌 完整地址：</b>{escape_text(name)}<br/>
<b>🏠 道路：</b>{escape_text(road) or '无'}<br/>
<b>🏙️ 城市：</b>{escape_text(city) or '无'}<br/>
<b>🗺️ 县/区：</b>{escape_text(county) or '无'}<br/>
<b>🏛️ 州/省：</b>{escape_text(state) or '无'}<br/>
<b>🌍 国家：</b>{escape_text(country) or '无'}<br/>
<b>📮 邮编：</b>{escape_text(postcode) or '无'}<br/>
"""
                return summary, details_html
            else:
                summary = "❌ 地理编码失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "📍 地理编码"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "search_poi":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                results = data.get("results", [])
                if not results:
                    summary = "📍 未找到结果"
                    details_html = "附近未找到符合条件的地点。"
                    return summary, details_html
                summary = f"📍 找到 {len(results)} 个结果"
                center_lat = results[0]["lat"]
                center_lon = results[0]["lon"]
                markers = [{"lat": r["lat"], "lon": r["lon"]} for r in results[:10]]
                map_img_url = await _get_static_map_image(center_lat, center_lon, markers=markers, zoom=13)
                map_html = f'<img src="{map_img_url}"/>' if map_img_url else ""

                items_html = ""
                for idx, item in enumerate(results[:10]):
                    label = chr(65 + idx)
                    name = escape_text(item["name"])
                    addr = escape_text(item.get("address", ""))
                    dist = item.get("distance", 0)
                    lat, lon = item["lat"], item["lon"]
                    phone = escape_text(item.get("phone", ""))
                    website = escape_text(item.get("website", ""))
                    opening = escape_text(item.get("opening_hours", ""))
                    items_html += f"""
<li>
<b>{label}. {name}</b><br/>
地址：{addr}（约 {dist:.0f} 米）<br/>
"""
                    if phone:
                        items_html += f"📞 {phone}<br/>"
                    if website:
                        items_html += f"🌐 <a href=\"{website}\">{website}</a><br/>"
                    if opening:
                        items_html += f"🕒 {opening}<br/>"
                    items_html += "</li>"
                details_html = f"{map_html}<ul>{items_html}</ul>"
                return summary, details_html
            elif data.get("status") == "no_results":
                summary = "📍 未找到 POI"
                details_html = data.get("message", "附近未找到相关地点。")
                return summary, details_html
            else:
                summary = "❌ POI 搜索失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "📍 POI 结果"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "route":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                summary = f"🚗 {data['distance_km']} km, {data['duration_min']} min"
                steps_html = "".join(f"<li>{escape_text(s)}</li>" for s in data.get("steps", []))
                start_name = escape_text(data['start_name'])
                end_name = escape_text(data['end_name'])
                start_lat = data.get('start_lat', 0)
                start_lon = data.get('start_lon', 0)
                end_lat = data.get('end_lat', 0)
                end_lon = data.get('end_lon', 0)
                markers = [{"lat": start_lat, "lon": start_lon}, {"lat": end_lat, "lon": end_lon}]
                center_lat = (start_lat + end_lat) / 2
                center_lon = (start_lon + end_lon) / 2
                map_img_url = await _get_static_map_image(center_lat, center_lon, markers=markers, zoom=10)
                map_html = f'<img src="{map_img_url}"/>' if map_img_url else f'<tg-map lat="{start_lat}" long="{start_lon}" zoom="12"/>'
                details_html = f"""
{map_html}
<b>从</b> {start_name}<br/>
<b>到</b> {end_name}<br/>
📏 距离：{data['distance_km']} km<br/>
⏱️ 时间：{data['duration_min']} 分钟<br/>
📍 起点坐标：{start_lat:.6f}, {start_lon:.6f}<br/>
📍 终点坐标：{end_lat:.6f}, {end_lon:.6f}<br/>
<details><summary>详细步骤</summary><ol>{steps_html}</ol></details>
"""
                return summary, details_html
            else:
                summary = "❌ 路线规划失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "🚗 路线"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "distance":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                summary = f"📏 {data['distance_km']} km"
                from_lat, from_lon = data['from']['lat'], data['from']['lon']
                to_lat, to_lon = data['to']['lat'], data['to']['lon']
                map_center_lat = (from_lat + to_lat) / 2
                map_center_lon = (from_lon + to_lon) / 2
                markers = [{"lat": from_lat, "lon": from_lon}, {"lat": to_lat, "lon": to_lon}]
                map_img_url = await _get_static_map_image(map_center_lat, map_center_lon, markers=markers, zoom=10)
                map_html = f'<img src="{map_img_url}"/>' if map_img_url else f'<tg-map lat="{map_center_lat}" long="{map_center_lon}" zoom="10"/>'
                details_html = f"""
{map_html}
<b>两点直线距离</b><br/>
距离：{data['distance_km']} km（{data['distance_mi']} mi）<br/>
起点坐标：({from_lat:.6f}, {from_lon:.6f})<br/>
终点坐标：({to_lat:.6f}, {to_lon:.6f})<br/>
"""
                return summary, details_html
            else:
                summary = "❌ 距离计算失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "📏 距离"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "place_details":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                name = escape_text(data.get("name", "未命名"))
                phone = escape_text(data.get("phone", "无"))
                website = escape_text(data.get("website", ""))
                opening = escape_text(data.get("opening_hours", "无"))
                cuisine = escape_text(data.get("cuisine", "无"))
                wheelchair = escape_text(data.get("wheelchair", "无"))
                smoking = escape_text(data.get("smoking", "无"))
                internet = escape_text(data.get("internet_access", "无"))
                brand = escape_text(data.get("brand", ""))
                email = escape_text(data.get("email", ""))
                addr_full = escape_text(data.get("addr_full", ""))
                description = escape_text(data.get("description", ""))
                fee = escape_text(data.get("fee", ""))
                lat, lon = data.get("lat", 0), data.get("lon", 0)

                summary = f"📍 {name}"
                map_img_url = await _get_static_map_image(lat, lon, zoom=16)
                map_html = (f'<img src="{html.escape(str(map_img_url), quote=True)}"/>' if map_img_url
                            else f'<tg-map lat="{lat}" long="{lon}" zoom="16"/>')
                if website:
                    ws_esc = html.escape(str(website), quote=True)
                    website_link = f'<a href="{ws_esc}">{ws_esc}</a>'
                else:
                    website_link = "无"

                # 导航链接
                nav = data.get("nav_links", {})
                nav_parts = []
                # 关键：使用 html.escape(..., quote=True) 转义 URL，防止属性注入
                if nav.get("google"):
                    nav_parts.append(f'<a href="{html.escape(str(nav["google"]), quote=True)}">Google Maps</a>')
                if nav.get("gaode"):
                    nav_parts.append(f'<a href="{html.escape(str(nav["gaode"]), quote=True)}">高德地图</a>')
                if nav.get("baidu"):
                    nav_parts.append(f'<a href="{html.escape(str(nav["baidu"]), quote=True)}">百度地图</a>')
                nav_html = " · ".join(nav_parts) if nav_parts else ""

                # 逐行拼接，只显示有值的字段
                lines = [map_html, f"<b>{name}</b><br/>"]
                if brand:
                    lines.append(f"🏷️ 品牌：{brand}<br/>")
                if addr_full:
                    lines.append(f"📌 地址：{addr_full}<br/>")
                if phone != "无" and phone:
                    lines.append(f"📞 电话：{phone}<br/>")
                if email:
                    lines.append(f"📧 邮箱：{email}<br/>")
                if website:
                    lines.append(f"🌐 网站：{website_link}<br/>")
                if opening != "无" and opening:
                    lines.append(f"🕒 营业时间：{opening}<br/>")
                if cuisine != "无" and cuisine:
                    lines.append(f"🍽️ 菜系：{cuisine}<br/>")
                if fee:
                    lines.append(f"💰 收费：{fee}<br/>")
                if wheelchair != "无" and wheelchair:
                    lines.append(f"♿ 无障碍：{wheelchair}<br/>")
                if smoking != "无" and smoking:
                    lines.append(f"🚬 吸烟：{smoking}<br/>")
                if internet != "无" and internet:
                    lines.append(f"📶 Wi-Fi：{internet}<br/>")
                if description:
                    lines.append(f"📝 简介：{description}<br/>")
                if nav_html:
                    lines.append(f"🗺️ 导航：{nav_html}<br/>")

                details_html = "\n".join(lines)
                return summary, details_html
            else:
                summary = "❌ 地点详情失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "📍 地点详情"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "elevation":
        try:
            data = json.loads(result_str)
            if data.get("status") == "success":
                elev = data['elevation_m']
                lat, lon = data['lat'], data['lon']
                summary = f"⛰️ {elev:.1f} m"
                map_img_url = await _get_static_map_image(lat, lon, zoom=14)
                map_html = f'<img src="{map_img_url}"/>' if map_img_url else f'<tg-map lat="{lat}" long="{lon}" zoom="14"/>'
                details_html = f"""
{map_html}
<b>海拔信息</b><br/>
📍 坐标：{lat:.6f}, {lon:.6f}<br/>
⛰️ 海拔：{elev:.1f} 米<br/>
"""
                return summary, details_html
            else:
                summary = "❌ 海拔查询失败"
                details_html = escape_text(result_str)
                return summary, details_html
        except Exception:
            summary = "⛰️ 海拔"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "traffic":
        try:
            data = json.loads(result_str)
            status = data.get("status")
            if status == "unavailable":
                summary = "🚦 交通（未启用）"
                details_html = data.get("message", "需要配置 API 密钥。")
            elif status == "success":
                incidents = data.get("incidents", [])
                count = data.get("count", 0)
                summary = f"🚦 交通事件：{count} 起"
                if incidents:
                    rows = ""
                    for inc in incidents[:10]:
                        category = escape_text(inc.get("category", "未知"))
                        desc = escape_text(inc.get("description", ""))
                        severity = inc.get("severity", 0)
                        road = escape_text(inc.get("road", ""))
                        rows += f"<tr><td>{category}</td><td>{desc}</td><td>{severity}</td><td>{road}</td></tr>"
                    details_html = f"""
<table bordered striped>
<tr><th>类型</th><th>描述</th><th>严重度</th><th>道路</th></tr>
{rows}
</table>
"""
                else:
                    details_html = data.get("message", "周边无交通事件")
            else:
                summary = "❌ 交通查询失败"
                details_html = escape_text(result_str)
            return summary, details_html
        except Exception:
            summary = "🚦 交通"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "isochrone":
        try:
            data = json.loads(result_str)
            status = data.get("status")
            if status == "unavailable":
                summary = "⏱️ 等时圈（未启用）"
                details_html = data.get("message", "需要配置 API 密钥。")
            elif status == "success":
                area = data.get("area_sq_m", 0)
                reach = data.get("reach_factor", 0)
                time_min = data.get("time_minutes", 0)
                profile = data.get("profile", "driving")
                summary = f"⏱️ {time_min}分钟 {profile} 可达范围"
                details_html = f"""
<b>等时圈结果</b><br/>
⏱️ 时间：{time_min} 分钟<br/>
🚗 出行方式：{profile}<br/>
📐 面积：{area:.0f} 平方米<br/>
📈 可达因子：{reach:.2f}<br/>
<i>（多边形坐标已省略，仅展示概要）</i>
"""
            else:
                summary = "❌ 等时圈计算失败"
                details_html = escape_text(result_str)
            return summary, details_html
        except Exception:
            summary = "⏱️ 等时圈"
            details_html = escape_text(result_str)
            return summary, details_html

    elif fn_name == "text_editor":
        command = fn_args.get("command", "")
        path = fn_args.get("path", "")

        def render_editor_block(text: str) -> str:
            # Keep line breaks and indentation intact for tool output.
            # Using <pre> makes editor results much easier to read in the foldout.
            safe = escape_text(text or "")
            return (
                "<pre style=\"white-space: pre-wrap; word-break: break-word; "
                "margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;\">"
                f"{safe}</pre>"
            )

        if "Error:" in result_str:
            summary = "❌ 编辑器操作失败"
            details_html = render_editor_block(result_str)
        elif command == "view":
            lines = result_str.split("\n")
            if len(lines) > 20:
                truncated = "\n".join(lines[:20]) + f"\n... (共 {len(lines)} 行)"
                summary = f"📄 查看文件（前20行）"
                details_html = render_editor_block(truncated)
            else:
                summary = f"📄 查看文件（{len(lines)} 行）"
                details_html = render_editor_block(result_str)
        elif "Successfully" in result_str or "File created" in result_str:
            if command == "create":
                summary = f"📄 已创建文件 {path}" if path else "📄 已创建文件"
            elif command in ("str_replace", "replace_lines", "insert"):
                summary = f"📝 已编辑文件 {path}" if path else "📝 已编辑文件"
            elif command == "delete":
                summary = f"🗑️ 已删除文件 {path}" if path else "🗑️ 已删除文件"
            else:
                summary = "✅ 编辑器操作成功"
            details_html = render_editor_block(result_str)
        else:
            summary = "📝 编辑器操作"
            details_html = render_editor_block(result_str)
        return summary, details_html

    # ===================== Todo 工具格式化 =====================
    # execute_todo 返回 JSON 字符串（给 AI 阅读）。UI 这里把它渲染成富文本卡片：
    #   - 顶部统计：总数 / 已完成 / 待办
    #   - 列表项：状态 emoji + 优先级徽章 + 标题（完成则加删除线）+ 标签 chips
    #   - 长列表自动截断并提示
    # 注意：list 动作在 dispatch_tool_call 里已经额外推送了一条带 InlineKeyboard 的可交互消息；
    # 这里的 details_html 只是工具调用气泡里的折叠预览，两者共用 render_todo_card。
    elif fn_name == "todo":
        try:
            import json as _json
            payload = _json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "📋 待办操作"
            details_html = escape_text(result_str)
            return summary, details_html

        if not payload.get("ok"):
            summary = f"❌ 待办操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_text(payload.get('error', '未知错误'))}</p>"
            return summary, details_html

        action = payload.get("action", "list")
        if action == "list":
            todos = payload.get("todos", []) or []
            total = payload.get("total", 0)
            pending = payload.get("pending", 0)
            summary = f"📋 共 {total} 项 · 待办 {pending} 项"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "add":
            t = payload.get("todo", {})
            summary = f"➕ 新增 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action in ("done", "undone", "toggle"):
            t = payload.get("todo", {})
            icon = "✅" if t.get("done") else "↩️"
            summary = f"{icon} {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "delete":
            t = payload.get("todo", {})
            summary = f"🗑️ 删除 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "edit":
            t = payload.get("todo", {})
            summary = f"📝 编辑 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        summary = "📋 待办操作"
        details_html = render_todo_card(payload)
        return summary, details_html

    # ===================== Memory 工具格式化 =====================
    # execute_memory 返回 JSON 字符串（给 AI 阅读），这里渲染成富文本卡片。
    elif fn_name == "memory":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🧠 记忆操作"
            details_html = escape_text(result_str)
            return summary, details_html
        if not payload.get("ok"):
            summary = f"❌ 记忆操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_text(payload.get('error', '未知错误'))}</p>"
            return summary, details_html
        action = payload.get("action", "list")
        if action == "list":
            total = payload.get("total", 0)
            shown = payload.get("shown", 0)
            summary = f"🧠 记忆库：{total} 条 · 显示 {shown} 条"
        elif action == "search":
            summary = f"🔎 记忆搜索：{payload.get('matches', 0)} / {payload.get('total', 0)} 条命中"
        elif action == "add":
            m = payload.get("memory", {})
            summary = f"🧠 保存 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "get":
            m = payload.get("memory", {})
            summary = f"🧠 查看 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "update":
            m = payload.get("memory", {})
            summary = f"📝 更新 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "delete":
            m = payload.get("memory", {})
            summary = f"🗑️ 删除 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条记忆"
        else:
            summary = "🧠 记忆操作"
        details_html = render_memory_card(payload)
        return summary, details_html

    # ===================== Subagent 工具格式化 =====================
    # execute_subagent 返回 JSON，含 answer / rounds / tool_calls / elapsed。
    # 父 agent 在工具气泡里看到完整子 agent 答复；用户也能从气泡折叠区阅读。
    elif fn_name == "subagent":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🤖 子 agent"
            details_html = escape_text(result_str)
            return summary, details_html
        ok = payload.get("ok", False)
        model_name = payload.get("model_name") or payload.get("model") or "?"
        rounds = payload.get("rounds", 0)
        tool_calls = payload.get("tool_calls", 0)
        elapsed = payload.get("elapsed", 0)
        if ok:
            summary = f"🤖 子 agent 完成 · {rounds} 轮 · {tool_calls} 工具 · {elapsed:.1f}s"
        else:
            err = payload.get("error", "未知错误")
            summary = f"❌ 子 agent 失败 · {rounds} 轮 · {err[:40]}"
        details_html = render_subagent_card(payload)
        return summary, details_html

    # ===================== Bash 工具格式化 =====================
    elif fn_name == "bash":
        if "Error:" in result_str or "Command rejected" in result_str:
            summary = "❌ Bash 执行失败"
        else:
            cmd_line = result_str.split("\n")[0].replace("Command: ", "")
            if len(cmd_line) > 30:
                cmd_line = cmd_line[:30] + "..."
            summary = f"🖥 {cmd_line}"
        details_html = f"<pre><code>{escape_text(result_str)}</code></pre>"
        return summary, details_html
    elif fn_name == "present_files":
        # ---- Decoupled data abstraction ----
        # execute_present_files returns a JSON payload:
        #   {"sent": [...], "failed": [...], "error": str | null}
        # The model context receives this raw JSON (so it can reply concisely,
        # e.g. "Files sent"), while the UI gets a rich, detailed report built
        # from the parsed structure.
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if not isinstance(data, dict):
            # Legacy fallback: result_str was not JSON (e.g. an error string
            # from dispatch_tool_call's top-level exception handler). Render
            # it as escaped plain text so we never break the UI.
            summary = "📂 Presenting files"
            details_html = escape_text(result_str) or "<i>No files were processed.</i>"
            return summary, details_html

        sent = data.get("sent") or []
        failed = data.get("failed") or []
        error = data.get("error")
        # Be defensive: ensure both lists are actually lists.
        if not isinstance(sent, list):
            sent = []
        if not isinstance(failed, list):
            failed = []

        sent_count = len(sent)
        failed_count = len(failed)

        # ---- Summary with correct pluralization (guards None / 0) ----
        if error and sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 1:
            summary = "📂 Presented 1 file"
        else:
            summary = f"📂 Presented {sent_count} files"

        # ---- Details: HTML list of successes and failures ----
        details_parts: List[str] = []
        if sent:
            items = "".join(f"<li>{escape_text(str(f))}</li>" for f in sent)
            label = "file" if sent_count == 1 else "files"
            details_parts.append(f"<b>✅ Sent ({sent_count} {label})</b><ul>{items}</ul>")
        if failed:
            items = "".join(f"<li>{escape_text(str(f))}</li>" for f in failed)
            label = "file" if failed_count == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({failed_count} {label})</b><ul>{items}</ul>")
        if error:
            details_parts.append(f"<i>{escape_text(str(error))}</i>")

        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")

        details_html = "<br/>".join(details_parts)
        return summary, details_html
    else:
        summary = f"🔧 {fn_name}"
        details_html = escape_text(result_str)
        return summary, details_html

async def execute_present_files(chat_id: int, paths: List[str]) -> str:
    if not paths:
        return json.dumps({"sent": [], "failed": [], "error": "No paths provided."})

    lock = await _get_workspace_lock(chat_id)
    async with lock:
        # 1. 从 R2 同步最新文件到本地
        await _sync_workspace_from_r2(chat_id)

        workspace = workspace_root(chat_id)
        workspace_workdir(chat_id)
        sent = []
        failed = []
        # 文件大小上限：50MB，防止 OOM
        _MAX_PRESENT_FILE_SIZE = 50 * 1024 * 1024
        for path in paths:
            if not isinstance(path, str) or not path:
                failed.append(f"{path} (invalid path)")
                continue
            # 拒绝嵌入的 null 字节
            if "\x00" in path:
                failed.append(f"{path} (invalid path)")
                continue
            safe_path = os.path.normpath(path)
            if safe_path == "." or safe_path.startswith("..") or os.path.isabs(safe_path):
                failed.append(f"{path} (invalid path)")
                continue
            local_path = workspace / safe_path
            # 关键：使用 resolve() 跟随符号链接，再校验最终路径仍在 workspace 之下
            try:
                resolved = local_path.resolve()
            except Exception:
                failed.append(f"{path} (invalid path)")
                continue
            try:
                workspace_resolved = workspace.resolve()
            except Exception:
                workspace_resolved = workspace
            if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
                failed.append(f"{path} (invalid path)")
                continue
            if not resolved.is_file():
                failed.append(f"{path} (file not found)")
                continue
            try:
                file_size = resolved.stat().st_size
                if file_size > _MAX_PRESENT_FILE_SIZE:
                    failed.append(f"{path} (file too large: {file_size} bytes)")
                    continue
                # 使用 asyncio.to_thread 包装同步 read，避免阻塞事件循环
                file_data = await asyncio.to_thread(resolved.read_bytes)
                # 显式超时，防止 hang 死
                timeout = aiohttp.ClientTimeout(total=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("document", file_data, filename=resolved.name)
                    async with session.post(f"{BASE_URL}/sendDocument", data=form) as resp:
                        if resp.status == 200:
                            sent.append(resolved.name)
                        else:
                            failed.append(f"{path} (send failed: {resp.status})")
            except Exception as e:
                failed.append(f"{path} (error: {str(e)[:50]})")
        return json.dumps({"sent": sent, "failed": failed, "error": None})

# ---------- 工具分发 ----------
async def dispatch_tool_call(name: str, arguments: dict, chat_id: int, progress_callback=None) -> str:
    if chat_id is None:
        # 早期失败：避免创建 ./workspace/None 造成跨会话数据泄漏
        return json.dumps({"error": "chat_id is required for tool dispatch"})
    try:
        if name == "web_search":
            return await execute_web_search(arguments.get("query", ""), arguments.get("num_results", 5))
        elif name == "fetch_url":
            # 增加重试逻辑：如果超时，重试一次
            url = arguments.get("url", "")
            for attempt in range(2):  # 最多尝试2次
                try:
                    return await execute_fetch_url(url)
                except asyncio.TimeoutError:
                    if attempt == 0:
                        logger.warning(f"fetch_url timeout, retrying (url={url})")
                        await asyncio.sleep(1)
                        continue
                    else:
                        # 重试后仍超时，返回友好消息
                        return "⏱️ 页面抓取超时，该网站可能响应较慢，建议稍后重试或手动访问。"
                except Exception as e:
                    logger.exception(f"fetch_url unexpected error: {e}")
                    return "⚠️ 页面抓取失败，请稍后重试或检查URL。"
            return "⚠️ 页面抓取失败，请稍后重试。"
        elif name == "skill_catalog":
            return skill_catalog_text()
        elif name == "skill_read":
            return skill_read_text(arguments.get("skill_id", ""))
        elif name == "skill_activate":
            skill_id_arg = arguments.get("skill_id", "")
            payload = skill_activate_skill(skill_id_arg, include_body=arguments.get("include_body", True))
            if "error" not in payload:
                # 用规范化后的真实 skill_id（而非用户可能传入的 name 别名），
                # 保证和 sync_skill_assets_to_workspace() 落盘的目录名、以及
                # bash 分支读取 active_skill 后拼接的 .skills/<id>/ 完全一致。
                resolved_skill_id = str(
                    (payload.get("activated") or {}).get("skill_id") or skill_id_arg
                )
                # ★ 手动激活同样必须同步资源到 workspace/.skills/<id>/，
                # 否则模型即使读到了 SKILL.md 正文，bash/text_editor 依然
                # 因为 Landlock 拒绝而访问不到 scripts/、REFERENCE.md 等文件。
                try:
                    sync_result = await asyncio.to_thread(
                        _skill_sync_assets, resolved_skill_id, workspace_root(chat_id)
                    )
                    payload["assets_sync"] = sync_result
                except Exception as e:
                    payload["assets_sync"] = {"synced": False, "error": str(e)}
                # 同步写入 active_skill 状态，保证与 ai_handlers.py 的自动匹配路径
                # 语义一致：下一次 bash 调用能读到这次手动激活，cwd 才会切到
                # 该 skill 目录。不这么做的话，模型手动 skill_activate 之后，
                # bash 依旧会落在 workspace 根，SKILL.md 的相对路径又对不上。
                try:
                    from apitelegramchat import state as _state
                    ctx = _state.user_contexts.setdefault(chat_id, {})
                    ctx["active_skill"] = {
                        "skill_id": resolved_skill_id,
                        "reason": "手动激活 (skill_activate)",
                        "score": None,
                        "updated_at": time.time(),
                    }
                except Exception:
                    pass
            return json.dumps(payload, ensure_ascii=False, indent=2)
        elif name == "wikipedia":
            return await execute_wikipedia(arguments.get("query", ""), arguments.get("lang", "zh"))
        elif name == "exchange_rate":
            return await execute_exchange_rate(arguments.get("base", "USD"), arguments.get("target"))
        elif name == "hacker_news":
            return await execute_hacker_news(arguments.get("limit", 10))
        elif name == "book_lookup":
            return await execute_book_lookup(arguments.get("query", ""))
        elif name == "weather":
            return await execute_weather(arguments.get("city", ""), arguments.get("unit", "c"),
                                         arguments.get("hours", 6))
        elif name == "news":
            return await execute_news(arguments.get("source", "bbc"), arguments.get("limit", 5))
        elif name == "crypto_price":
            return await execute_crypto_price(arguments.get("coin", ""), arguments.get("currency", "usd"))
        elif name == "ip_geo":
            return await execute_ip_geo(arguments.get("ip"))
        elif name == "qr_code":
            return await execute_qr_code(arguments.get("text", ""))
        elif name == "done":
            return await execute_done()
        elif name == "generate_image_from_text":
            return await execute_generate_image(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=None  # 强制无参考图
            )
        elif name == "edit_image_with_reference":
            return await execute_generate_image(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=arguments.get("image_url")  # 带参考图
            )
        elif name == "generate_video":
            return await execute_generate_video(
                prompt=arguments.get("prompt"),
                model=arguments.get("model"),
                duration=arguments.get("duration", 5),
                chat_id=chat_id,
            )
        elif name == "image_search":
            return await execute_image_search(
                arguments.get("query", ""),
                arguments.get("num_results", 3)
            )
        # 地图工具
        elif name == "geocode":
            return await execute_geocode(arguments.get("address", ""))
        elif name == "search_poi":
            return await execute_search_poi(
                arguments.get("lat", 0),
                arguments.get("lon", 0),
                arguments.get("query", ""),
                arguments.get("radius", 1000)
            )
        elif name == "route":
            return await execute_route(
                arguments.get("start", ""),
                arguments.get("end", ""),
                arguments.get("profile", "driving")
            )
        elif name == "distance":
            return await execute_distance(
                arguments.get("from_lat", 0),
                arguments.get("from_lon", 0),
                arguments.get("to_lat", 0),
                arguments.get("to_lon", 0)
            )
        elif name == "place_details":
            return await execute_place_details(
                arguments.get("query", ""),
                arguments.get("lat"),
                arguments.get("lon")
            )
        elif name == "elevation":
            return await execute_elevation(arguments.get("lat", 0), arguments.get("lon", 0))
        elif name == "traffic":
            return await execute_traffic(arguments.get("lat", 0), arguments.get("lon", 0),
                                         arguments.get("radius", 5000))
        elif name == "isochrone":
            return await execute_isochrone(arguments.get("lat", 0), arguments.get("lon", 0), arguments.get("time", 10),
                                           arguments.get("profile", "driving"))
        elif name == "text_editor":
            return await execute_text_editor(
                chat_id=chat_id,
                command=arguments.get("command", ""),
                path=arguments.get("path", ""),
                view_range=arguments.get("view_range"),
                old_str=arguments.get("old_str"),
                new_str=arguments.get("new_str"),
                start_line=arguments.get("start_line"),
                end_line=arguments.get("end_line"),
                occurrence=arguments.get("occurrence", 1),
                allow_multi=arguments.get("allow_multi", False),
                use_regex=arguments.get("use_regex", False),
                delete_range=arguments.get("delete_range"),
                insert_line=arguments.get("insert_line"),
                insert_text=arguments.get("insert_text"),
                file_text=arguments.get("file_text"),
                confirm=arguments.get("confirm", False),
            )
        # ========== Bash 工具分支 ==========
        elif name == "bash":
            # 读取当前 chat 的 active skill 作为模型上下文，但不替模型改变 cwd。
            # 使用 skill 时由模型自己 `cd .skills/<id>`；persistent bash 会保留该 cwd。
            active_skill_id = None
            try:
                from apitelegramchat import state as _state
                ctx = _state.user_contexts.get(chat_id, {})
                active_skill = ctx.get("active_skill")
                if isinstance(active_skill, dict):
                    active_skill_id = active_skill.get("skill_id")
            except Exception:
                active_skill_id = None
            return await execute_bash(
                chat_id=chat_id,
                command=arguments.get("command", ""),
                restart=arguments.get("restart", False),
                skill_id=active_skill_id,
                progress_callback=progress_callback,
            )
        # ========== Todo 工具分支 ==========
        # 任务 / 待办清单。返回 JSON 字符串给 AI 上下文；UI 渲染由 format_tool_result 处理。
        elif name == "todo":
            result_str = await execute_todo(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                title=arguments.get("title"),
                todo_id=arguments.get("todo_id"),
                priority=arguments.get("priority"),
                tags=arguments.get("tags"),
                note=arguments.get("note"),
                filter=arguments.get("filter"),
                tag=arguments.get("tag"),
            )
            return result_str
        # ========== Memory 工具分支 ==========
        # 长期记忆库。返回 JSON 给 AI；UI 由 format_tool_result 渲染富文本卡片。
        elif name == "memory":
            return await execute_memory(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                content=arguments.get("content"),
                memory_id=arguments.get("memory_id"),
                category=arguments.get("category"),
                tags=arguments.get("tags"),
                importance=arguments.get("importance"),
                query=arguments.get("query"),
                scope=arguments.get("scope"),
                limit=arguments.get("limit", 50),
                source=arguments.get("source", "agent"),
            )
        # ========== Subagent 工具分支 ==========
        # 子 agent。返回 JSON（含 answer / rounds / tool_calls）给父 agent 阅读；
        # UI 由 format_tool_result 渲染成「子 agent 已完成」卡片。
        # progress_callback 让子 agent 每轮能向主 agent 的草稿推送进度，避免 90s 黑屏。
        elif name == "subagent":
            return await execute_subagent(
                chat_id=chat_id,
                task=arguments.get("task", ""),
                context=arguments.get("context"),
                model=arguments.get("model"),
                allowed_tools=arguments.get("allowed_tools"),
                timeout=arguments.get("timeout"),
                progress_callback=progress_callback,
            )
        elif name == "present_files":
            paths = arguments.get("paths", [])
            if isinstance(paths, str):
                paths = [paths]
            return await execute_present_files(chat_id, paths)
        else:
            return f"失败：未知工具: {name}。"
    except asyncio.CancelledError:
        # 关键：CancelledError 必须向上传播，否则用户发新消息无法取消正在执行的工具调用，
        # agentic 循环会把取消信号当成普通工具失败吞掉，导致旧任务继续跑。
        raise
    except Exception as e:
        # 顶层异常：只记录日志，返回用户友好消息，不暴露内部细节
        logger.exception(f"dispatch_tool_call 顶层异常 [{name}]: {e}")
        return "⚠️ 工具执行出错，请稍后重试或换一种方式。"
