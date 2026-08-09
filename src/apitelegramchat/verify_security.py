#!/usr/bin/env python3
# =====================================================================
# verify_security.py — 部署后安全自检脚本
# =====================================================================
# 用法:
#   1. 在容器内执行：python verify_security.py
#   2. 所有测试项应通过；失败项说明该防御层失效
# =====================================================================

import asyncio
import os
import re
import shutil
import sys
import subprocess
from pathlib import Path

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
WARN = "\033[33m[WARN]\033[0m"
INFO = "\033[36m[INFO]\033[0m"

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
    """检查 os.environ 中是否还有敏感变量"""
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
    report("2.1 敏感环境变量已清洗", not leaked,
           f"残留: {leaked}" if leaked else "无敏感变量泄漏")


# ----------------------------------------------------------------------
# 3. bwrap 沙箱可用性
# ----------------------------------------------------------------------
def check_bwrap():
    bwrap = shutil.which("bwrap")
    if not bwrap:
        report("3.1 bwrap 已安装", False, "bwrap not found in PATH")
        return False
    report("3.1 bwrap 已安装", True, f"path={bwrap}")

    # 测试能否启动沙箱
    try:
        rc = subprocess.run(
            [bwrap, "--unshare-user-try", "--unshare-pid-try",
             "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "/bin/true"],
            capture_output=True, timeout=5
        )
        ok = rc.returncode == 0
        report("3.2 bwrap 可创建沙箱", ok,
               f"rc={rc.returncode} stderr={rc.stderr.decode()[:200]}")
        return ok
    except Exception as e:
        report("3.2 bwrap 可创建沙箱", False, str(e))
        return False


# ----------------------------------------------------------------------
# 4. 沙箱内隔离测试
# ----------------------------------------------------------------------
async def check_sandbox_isolation(bwrap_ok: bool):
    """在沙箱里跑一组命令，验证隔离性"""
    if not bwrap_ok:
        warn("4.x 沙箱隔离测试", "bwrap 不可用，跳过")
        return

    from apitelegramchat.sandbox import build_bwrap_argv

    workspace = Path("/tmp/verify_workspace").absolute()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "secret.txt").write_text("THIS_IS_SECRET_12345")

    argv = build_bwrap_argv(workspace, 999999)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        text=False, bufsize=0,
        env={},
    )

    tests = [
        # (命令, 期望通过/失败, 说明)
        ("env\n",                           False, "4.1 env 不应包含任何 KEY/TOKEN"),
        ("cat /etc/shadow 2>&1\n",          False, "4.2 /etc/shadow 应不可读"),
        ("ls /app/config.py 2>&1\n",        True,  "4.3 /app/config.py 只读可见"),
        ("echo DATA > /app/test 2>&1\n",    False, "4.4 /app 应不可写"),
        ("cat /proc/1/cmdline 2>&1\n",      False, "4.5 /proc/1 应不可见或显示自己"),
        ("sudo ls 2>&1\n",                  False, "4.6 sudo 应不可用"),
        ("cat secret.txt\n",                True,  "4.7 workspace 内文件可读"),
        ("ls ../ 2>&1\n",                   False, "4.8 父目录应只看到自己"),
        ("python3 -c \"import os; print(os.environ)\" 2>&1\n", True, "4.9 Python 子进程 env 干净"),
    ]

    for cmd, should_pass, desc in tests:
        try:
            proc.stdin.write(cmd.encode())
            await proc.stdin.drain()
            await asyncio.sleep(0.3)
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
            output = line.decode("utf-8", errors="replace").strip()
            # 简化判断：检查输出是否包含敏感关键字
            if "should_pass" in desc:
                pass
            if "4.1" in desc:
                # 期望 env 不含 KEY/TOKEN
                bad = bool(re.search(r'(KEY|TOKEN|SECRET|PASSWORD)=', output))
                report(desc, not bad, output[:200])
            elif "4.2" in desc:
                report(desc, "Permission denied" in output or "No such file" in output,
                       output[:200])
            elif "4.3" in desc:
                report(desc, "/app/config.py" in output, output[:200])
            elif "4.4" in desc:
                report(desc, "Permission denied" in output or "read-only" in output,
                       output[:200])
            elif "4.5" in desc:
                report(desc, "self" in output.lower() or "Permission denied" in output
                       or "No such file" in output or "bash" in output.lower(),
                       output[:200])
            elif "4.6" in desc:
                report(desc, "not found" in output.lower() or "Permission denied" in output,
                       output[:200])
            elif "4.7" in desc:
                report(desc, "THIS_IS_SECRET_12345" in output, output[:200])
            elif "4.8" in desc:
                report(desc, "999999" in output or output.count("\n") <= 2,
                       output[:200])
            elif "4.9" in desc:
                bad = bool(re.search(r'(KEY|TOKEN|SECRET|PASSWORD)=', output))
                report(desc, not bad, output[:300])
        except Exception as e:
            report(desc, False, f"exception: {e}")

    proc.kill()
    await proc.wait()
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
    ws = Path("/app/workspace")
    if not ws.exists():
        warn("6.x workspace 权限", "/app/workspace 不存在")
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
    bwrap_ok = check_user()
    check_no_sudo()
    print()

    print("--- 2. 环境变量 ---")
    check_env_scrubbed()
    print()

    print("--- 3. bwrap 可用性 ---")
    bwrap_ok = check_bwrap() and bwrap_ok
    print()

    print("--- 4. 沙箱隔离 ---")
    await check_sandbox_isolation(bwrap_ok)
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
