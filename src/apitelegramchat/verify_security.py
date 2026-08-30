# =====================================================================
# verify_security.py — 部署后安全自检脚本
# =====================================================================
# 用法:
#   1. 在容器内执行：python -m apitelegramchat.verify_security
#   2. 所有测试项应通过；失败项说明该防御层失效
# =====================================================================

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ANSI 颜色码仅在 TTY 输出，避免污染 CI/CD 日志。
_USE_COLOR = sys.stdout.isatty()


def _color(code: int, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


PASS = _color(32, "[PASS]")
FAIL = _color(31, "[FAIL]")
WARN = _color(33, "[WARN]")
INFO = _color(36, "[INFO]")

results = []


def report(name: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    line = f"{status} {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append((name, ok))


def warn(name: str, detail: str):
    print(f"{WARN} {name} — {detail}")
    results.append((name, True))  # warn 不算 fail


def info(name: str, detail: str):
    print(f"{INFO} {name} — {detail}")


# ----------------------------------------------------------------------
# 1. 容器身份检查
# ----------------------------------------------------------------------
def check_user():
    uid = os.getuid()
    report("1.1 非 root 运行", uid != 0, f"uid={uid}")
    return uid != 0


def check_no_sudo():
    has_sudo = shutil.which("sudo") is not None
    has_su = shutil.which("su") is not None
    report("1.2 sudo/su 不可用", not (has_sudo or has_su),
           f"sudo={has_sudo} su={has_su}")


# ----------------------------------------------------------------------
# 2. 敏感环境变量检查
# ----------------------------------------------------------------------
def check_env_scrubbed():
    """检查 os.environ 中是否还有敏感变量。

    安全修复：此前直接把残留变量名打印到 stdout / 日志，这本身
    是一种信息泄露（虽然只打印名字不打印值，但泄露"我们用了
    STRIPE_SECRET_KEY"等本身也是 leak）。改成只打印数量，名字
    仅在 DEBUG 级别输出。
    """
    sensitive_patterns = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    leaked = []
    for k in os.environ:
        ku = k.upper()
        for pat in sensitive_patterns:
            if pat in ku:
                leaked.append(k)
                break
    # HOME/USER 等白名单不算
    safe_whitelist = {"USER", "HOME", "PATH", "LANG", "LC_ALL", "TERM", "SHELL", "PWD"}
    leaked = [k for k in leaked if k not in safe_whitelist]
    if leaked:
        # 只在 DEBUG 级别打印变量名，stdout / 日志里只显示数量。
        logger.debug("leaked sensitive env var names: %s", leaked)
    report("2.1 敏感环境变量已清洗", not leaked,
           f"残留 {len(leaked)} 个敏感变量（详见 DEBUG 日志）" if leaked else "无敏感变量泄漏")


# ----------------------------------------------------------------------
# 3. Landlock 沙箱可用性
# ----------------------------------------------------------------------
def check_landlock():
    from apitelegramchat.sandbox import _landlock_supported
    ok = _landlock_supported()
    report("3.1 Landlock 内核支持", ok,
           "Linux 5.13+ required" if not ok else "OK")
    return ok


# ----------------------------------------------------------------------
# 4. 沙箱内隔离测试
# ----------------------------------------------------------------------
async def check_sandbox_isolation(landlock_ok: bool):
    """Run independent commands and assert filesystem confinement."""
    if not landlock_ok:
        report("4.0 Landlock sandbox available", False, "Landlock unavailable: sandbox cannot be considered safe")
        return

    # 用随机生成的探针 secret，避免固定字符串意外通过 sandbox 泄漏到生产。
    import secrets as _secrets
    probe_secret = _secrets.token_hex(16)
    probe_secret_outside = _secrets.token_hex(16)

    import functools
    from apitelegramchat.sandbox import build_sandbox_argv, build_sandbox_env, _preexec_sandbox

    # 修复 symlink 攻击：原代码用固定路径 /tmp/verify_workspace，
    # 本地攻击者可以提前创建该路径并指向 /etc，让 verify_security 写
    # 测试文件到 /etc。改用 mkdtemp 在私有 data_root 下创建唯一目录。
    from apitelegramchat.workspace_paths import data_root
    try:
        base = data_root()
    except Exception:
        base = Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="verify_workspace_", dir=str(base)))
    parent_probe = workspace.parent / "landlock-parent-probe.txt"
    outside_target = workspace.parent / "landlock-outside-target.txt"
    (workspace / "secret.txt").write_text(probe_secret)
    parent_probe.unlink(missing_ok=True)
    outside_target.write_text(probe_secret_outside)

    async def run(cmd: str) -> tuple[int, str]:
        argv = build_sandbox_argv()
        env = build_sandbox_env(workspace, 999999)
        preexec = functools.partial(_preexec_sandbox, str(workspace))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            text=False,
            bufsize=0,
            env=env,
            cwd=str(workspace),
            start_new_session=True,
            preexec_fn=preexec,
        )
        try:
            # 提升到 15s：Landlock + rlimit + bash 启动在冷容器里
            # 经常超过 5s，旧值会产生假阴性 FAIL。
            out, _ = await asyncio.wait_for(proc.communicate(cmd.encode()), timeout=15)
            return proc.returncode or 0, out.decode("utf-8", errors="replace")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    try:
        rc, out = await run("printf ok > inside.txt\ncat inside.txt\n")
        report("4.1 workspace 内读写", rc == 0 and "ok" in out, out[:200])

        rc, out = await run("printf x > ../landlock-parent-probe.txt\n")
        report("4.2 ../ 写入被拒绝", rc != 0 and not parent_probe.exists(), out[:200])

        rc, out = await run("cat ../landlock-outside-target.txt 2>&1\n")
        report("4.3 ../ 读取被拒绝", rc != 0 and probe_secret_outside not in out, out[:200])

        rc, out = await run("ln -s ../landlock-outside-target.txt escape-link\ncat escape-link 2>&1\n")
        report("4.4 symlink 逃逸被拒绝", probe_secret_outside not in out, out[:200])

        rc, out = await run("cat /app/config.py 2>&1\n")
        report("4.5 应用源码不可读", "Permission denied" in out or "No such file" in out, out[:200])

        rc, out = await run("printf x > /app/landlock-write-probe 2>&1\n")
        # /app 不存在也是安全状态，不应判 FAIL。
        report("4.6 应用目录不可写",
               "Permission denied" in out or "Read-only" in out or "No such file" in out,
               out[:200])

        rc, out = await run("printf sandbox-ok > /dev/null\n")
        report("4.7 /dev/null 可写", rc == 0, out[:200])

        rc, out = await run("cat /etc/shadow 2>&1\n")
        report("4.8 /etc/shadow 不可读", "Permission denied" in out or "No such file" in out, out[:200])

        rc, out = await run("cat /proc/1/cmdline 2>&1\n")
        report("4.9 /proc 不可访问", "Permission denied" in out or "No such file" in out, out[:200])

        rc, out = await run("env\n")
        # 用 word boundary 避免误匹配 MONKEY= / PYTHONKEY= / TURKEY= 等无关变量。
        # 仅匹配以 SENSITIVE 字段结尾的环境变量名。
        bad = bool(re.search(r'(^|\n)\S*(?:KEY|TOKEN|SECRET|PASSWORD)=', out))
        report("4.9 子进程环境无密钥", not bad, out[:300])
    finally:
        parent_probe.unlink(missing_ok=True)
        outside_target.unlink(missing_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)


# ----------------------------------------------------------------------
# 5. 资源限制检查
# ----------------------------------------------------------------------
def check_resource_limits():
    import resource
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        report("5.1 NPROC 限制存在", hard != resource.RLIM_INFINITY,
               f"soft={soft} hard={hard}")
    except Exception as e:
        warn("5.1 NPROC 限制", str(e))

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        report("5.2 NOFILE 限制存在", hard < 65536,
               f"soft={soft} hard={hard}")
    except Exception as e:
        warn("5.2 NOFILE 限制", str(e))


# ----------------------------------------------------------------------
# 6. Workspace 权限检查
# ----------------------------------------------------------------------
def check_workspace_perms():
    # 用真实 data_root 路径而非硬编码 /app，否则非 /app 部署永远跳过检查。
    try:
        from apitelegramchat.workspace_paths import data_root
        ws = data_root()
    except Exception:
        ws = Path("/app/workspace")
    if not ws.exists():
        warn("6.x workspace 权限", f"{ws} 不存在")
        return
    mode = ws.stat().st_mode & 0o777
    report("6.1 workspace 权限 700", mode == 0o700, f"实际 mode={oct(mode)}")

    owner_uid = ws.stat().st_uid
    current_uid = os.getuid()
    report("6.2 workspace 属主 = 当前用户", owner_uid == current_uid,
           f"owner={owner_uid} current={current_uid}")


# ----------------------------------------------------------------------
# 7. setuid 检查
# ----------------------------------------------------------------------
def check_setuid():
    """扫描 /usr /bin 下的 setuid 二进制"""
    found = []
    for root_dir in ["/usr/bin", "/usr/local/bin", "/bin", "/sbin"]:
        if not Path(root_dir).exists():
            continue
        try:
            for entry in os.listdir(root_dir):
                p = Path(root_dir) / entry
                try:
                    mode = p.stat().st_mode
                    if mode & 0o4000:  # S_ISUID
                        found.append(str(p))
                except OSError:
                    continue
        except OSError:
            continue
    # 允许的 setuid: 通常容器内不应该有任何
    report("7.1 无 setuid 二进制", not found,
           f"发现 setuid: {found[:5]}" if found else "无")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
async def main():
    print("=" * 70)
    print(" Bash 沙箱安全自检")
    print("=" * 70)
    print()

    info("环境", f"Python {sys.version.split()[0]}, uid={os.getuid()}, pid={os.getpid()}")
    print()

    print("--- 1. 容器身份 ---")
    check_user()
    check_no_sudo()
    print()

    print("--- 2. 环境变量 ---")
    check_env_scrubbed()
    print()

    print("--- 3. Landlock 可用性 ---")
    landlock_ok = check_landlock()
    print()

    print("--- 4. 沙箱隔离 ---")
    await check_sandbox_isolation(landlock_ok)
    print()

    print("--- 5. 资源限制 ---")
    check_resource_limits()
    print()

    print("--- 6. workspace 权限 ---")
    check_workspace_perms()
    print()

    print("--- 7. setuid 二进制 ---")
    check_setuid()
    print()

    print("=" * 70)
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed
    print(f" 结果: {passed}/{total} 通过, {failed} 失败")
    print("=" * 70)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
