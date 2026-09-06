# =====================================================================
# tests/test_whitelist_r2.py — 白名单 R2 同步 + 权限边界 全量回归测试
# =====================================================================
# 运行方式（无需 pytest，直接 python 执行）：
#   cd <项目根>
#   python tests/test_whitelist_r2.py
#
# 覆盖范围：
#   A. 归一化 / 管理员目标判断（大小写、@ 前缀、数字 ID）
#   B. 白名单文件解析健壮性（BOM / CRLF / 空行 / 管理员条目过滤 / 非法条目）
#   C. 本地模式（R2 未配置）：文件即数据源，增删落盘、重启恢复
#   D. R2 模式（fake R2 后端）：启动拉取、修改推送、重启无复活、
#      播种迁移、网络故障回退、推送失败自愈、并发一致性
#   E. 授权边界：管理员不可被加入/删除用户白名单、大小写授权、
#      user_id 精确匹配、内存 set 引用恒定（历史 bug 回归）
# =====================================================================

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ---- 在导入任何项目模块之前，把数据目录指到独立临时目录 ----
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="wl_r2_test_"))
os.environ["APITELEGRAMCHAT_DATA_DIR"] = str(_TEST_ROOT / "data")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from apitelegramchat import config, s3_utils  # noqa: E402

PASS, FAIL = 0, 0
_failures = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        _failures.append(f"{name} {detail}")
        print(f"  [FAIL] {name} — {detail}")


def section(title: str):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------
# Fake R2 后端：替换 s3_utils 的三个入口，验证 config 层的同步逻辑
# ---------------------------------------------------------------------
class FakeR2:
    def __init__(self):
        self.reset()

    def reset(self):
        self.objects = {}          # key -> bytes
        self.fail_upload = False   # True: put_object 失败（返回 None）
        self.fail_download = False # True: get_object 抛异常（网络故障）
        self.exists_answer = None  # None: 按 objects 判断；True/False: 强制
        self.uploads = 0
        self.downloads = 0
        self.exists_calls = 0

    async def upload(self, data, key, content_type="application/octet-stream"):
        self.uploads += 1
        if self.fail_upload:
            return None
        self.objects[key] = bytes(data)
        return f"https://fake.r2.example/{key}"

    async def download(self, key):
        self.downloads += 1
        if self.fail_download:
            raise RuntimeError("simulated network failure")
        return self.objects.get(key)  # 不存在返回 None

    async def exists(self, key):
        self.exists_calls += 1
        if self.exists_answer is not None:
            return self.exists_answer
        return key in self.objects


fake_r2 = FakeR2()

# 保存真实实现：本地回退路径测试要用未打补丁的 s3_utils IO
REAL_UPLOAD = s3_utils.upload_bytes_to_r2
REAL_DOWNLOAD = s3_utils.download_from_r2
REAL_EXISTS = s3_utils.file_exists_in_r2

# 白名单的 R2 key 常量（config 顶层定义）
KEY = config.WHITELIST_R2_KEY


def patch_r2_mode(on: bool):
    """打开/关闭 R2 模式（is_r2_configured + 三个 IO 入口）。"""
    s3_utils.is_r2_configured = (lambda: True) if on else (lambda: False)
    s3_utils.upload_bytes_to_r2 = fake_r2.upload
    s3_utils.download_from_r2 = fake_r2.download
    s3_utils.file_exists_in_r2 = fake_r2.exists


def local_file() -> Path:
    return Path(config._resolve_whitelist_path())


def reset_all(r2_mode: bool):
    """完全复位：内存 set、本地文件、fake R2、管理员名单。"""
    fake_r2.reset()
    config.WHITELIST_USERS.clear()
    config.ADMIN_USERS = ["dearella"]
    config._ADMIN_NAME_SET, config._ADMIN_ID_SET = config._build_admin_sets()
    try:
        local_file().unlink(missing_ok=True)
        Path(str(local_file()) + ".tmp").unlink(missing_ok=True)
    except Exception:
        pass
    patch_r2_mode(r2_mode)


async def t_a_normalization():
    section("A. 归一化与管理员目标判断")
    n = config._normalize_target
    check("A1 @前缀剥离+小写", n("@Alice") == "alice")
    check("A2 前后空白剥离", n("  BOB ") == "bob")
    check("A3 多个@剥离", n("@@carol") == "carol")
    check("A4 纯数字ID保持原样", n("123456789") == "123456789")
    check("A5 空串", n("") == "" and n(None) == "" and n("   ") == "")
    check("A6 admin精确匹配", config._is_admin_target("dearella") is True)
    check("A7 admin大小写不敏感", config._is_admin_target("DEARELLA") is True and config._is_admin_target("@Dearella") is True)
    check("A8 非管理员用户名", config._is_admin_target("dearella2") is False)
    check("A9 非管理员数字ID", config._is_admin_target("999999") is False)
    check("A10 管理员身份判断(username)", config.is_admin_identity(username="Dearella") is True)
    check("A11 管理员身份判断(user_id不匹配用户名)", config.is_admin_identity(user_id="dearella") is False)
    check("A12 非管理员身份", config.is_admin_identity("someuser", "42") is False)

    # 动态加入数字 ID 管理员
    config.ADMIN_USERS = ["dearella", "555000111"]
    config._ADMIN_NAME_SET, config._ADMIN_ID_SET = config._build_admin_sets()
    check("A13 数字ID管理员", config._is_admin_target("555000111") is True and config.is_admin_identity(user_id="555000111") is True)
    check("A14 数字ID管理员目标互斥", config._is_admin_target("555000112") is False)
    config.ADMIN_USERS = ["dearella"]
    config._ADMIN_NAME_SET, config._ADMIN_ID_SET = config._build_admin_sets()


async def t_b_parse():
    section("B. 白名单文件解析健壮性")
    raw = b"\xef\xbb\xbfAlice\r\nbob \n@CAROL\n\n   \ndearella\nDearella2 \n112233\nx y\tz\n"
    got = config._parse_whitelist_bytes(raw)
    check("B1 解析结果", got == {"alice", "bob", "carol", "112233", "dearella2"}, str(got))
    check("B2 管理员条目被过滤", "dearella" not in got)
    check("B3 空内容 -> 空集合", config._parse_whitelist_bytes(b"") == set())
    check("B4 全空白文件", config._parse_whitelist_bytes(b"\n\n  \n") == set())
    check("B5 非法UTF8不崩溃", isinstance(config._parse_whitelist_bytes(b"\xff\xfe alice"), set))


async def t_c_local_mode():
    section("C. 本地模式（R2 未配置）：文件即数据源")
    reset_all(r2_mode=False)
    check("C0 is_r2_configured=False", s3_utils.is_r2_configured() is False)

    # 启动加载：本地文件不存在 -> 空白名单
    await config.load_whitelist()
    check("C1 无文件时空白名单", config.WHITELIST_USERS == set())

    # 手工放置本地文件（模拟旧版本遗留数据）后加载
    local_file().parent.mkdir(parents=True, exist_ok=True)
    local_file().write_text("legacy1\nlegacy2\n", encoding="utf-8")
    await config.load_whitelist()
    check("C2 本地文件加载", config.WHITELIST_USERS == {"legacy1", "legacy2"})

    # 增删 + 落盘
    r = await config.add_whitelist_user("alice")
    check("C3 add新用户=added", r == config.ADD_ADDED, r)
    check("C4 内存已加", "alice" in config.WHITELIST_USERS)
    check("C5 本地文件已落盘", "alice" in local_file().read_text(encoding="utf-8"))
    r = await config.add_whitelist_user("alice")
    check("C6 重复add=exists", r == config.ADD_EXISTS, r)

    r = await config.add_whitelist_user("Dearella")
    check("C7 add管理员被拒", r == config.ADD_ADMIN_REJECTED, r)
    check("C8 管理员未入白名单", "dearella" not in config.WHITELIST_USERS)

    r = await config.add_whitelist_user("@BOB")
    check("C9 add@大写用户归一化", r == config.ADD_ADDED and config.WHITELIST_USERS == {"legacy1", "legacy2", "alice", "bob"})

    r = await config.remove_whitelist_user("ALICE")
    check("C10 大小写不敏感remove", r == config.REMOVE_REMOVED and "alice" not in config.WHITELIST_USERS)
    r = await config.remove_whitelist_user("alice")
    check("C11 remove不存在=missing", r == config.REMOVE_MISSING, r)
    r = await config.remove_whitelist_user("dearella")
    check("C12 remove管理员被拒", r == config.REMOVE_ADMIN_REJECTED, r)
    check("C13 本地文件与内存一致", local_file().read_text(encoding="utf-8").split() == sorted(config.WHITELIST_USERS))

    # 重启模拟：清空内存再加载
    config.WHITELIST_USERS.clear()
    await config.load_whitelist()
    check("C14 重启后恢复", config.WHITELIST_USERS == {"legacy1", "legacy2", "bob"})

    # 授权语义（大小写）
    check("C15 大写用户名授权", config.is_whitelisted_identity(username="BOB") is True)
    check("C16 未加名单拒绝", config.is_whitelisted_identity(username="carol") is False)
    check("C17 空身份拒绝", config.is_whitelisted_identity("", "") is False)

    # 并发增删（本地模式）
    config.WHITELIST_USERS.clear()
    local_file().unlink(missing_ok=True)
    await asyncio.gather(
        config.add_whitelist_user("u1"),
        config.add_whitelist_user("u2"),
        config.add_whitelist_user("u3"),
        config.remove_whitelist_user("u2"),
    )
    await config.load_whitelist()  # 从盘上重读校验一致性
    check("C18 并发增删后一致", config.WHITELIST_USERS == {"u1", "u3"}, str(config.WHITELIST_USERS))


async def t_d_r2_mode():
    section("D. R2 模式：拉取 / 推送 / 重启 / 播种 / 故障 / 并发")
    reset_all(r2_mode=True)

    # ---- D1 启动拉取（R2 已有数据） ----
    fake_r2.objects[KEY] = "alice\nbob\n@CAROL\ndearella\n".encode()
    await config.load_whitelist()
    check("D1 启动从R2加载+管理员过滤+归一化", config.WHITELIST_USERS == {"alice", "bob", "carol"}, str(config.WHITELIST_USERS))
    check("D2 本地缓存回写", local_file().exists() and local_file().read_text(encoding="utf-8").split() == ["alice", "bob", "carol"])

    # ---- D2 授权语义 ----
    check("D3 大小写授权", config.is_whitelisted_identity(username="ALICE") is True)
    check("D4 ID未加时拒绝", config.is_whitelisted_identity(user_id=" 112233 ") is False)
    await config.add_whitelist_user("112233")
    check("D5 ID加入后精确授权", config.is_whitelisted_identity(user_id="112233") is True)
    check("D6 ID带空白授权", config.is_whitelisted_identity(user_id=" 112233 ") is True)

    # ---- D3 修改即推送 ----
    uploads_before = fake_r2.uploads
    r = await config.add_whitelist_user("dave")
    check("D7 add=added", r == config.ADD_ADDED, r)
    check("D8 R2已推送全量", fake_r2.objects[KEY].decode().split() == sorted(config.WHITELIST_USERS), fake_r2.objects[KEY].decode())
    check("D9 推送次数+1", fake_r2.uploads == uploads_before + 1)

    uploads_before = fake_r2.uploads
    r = await config.add_whitelist_user("Dearella")
    check("D10 add管理员被拒(R2模式)", r == config.ADD_ADMIN_REJECTED, r)
    check("D11 被拒时无推送", fake_r2.uploads == uploads_before)
    check("D12 R2未被污染", "dearella" not in fake_r2.objects[KEY].decode())

    r = await config.remove_whitelist_user("bob")
    check("D13 del=removed+推送", r == config.REMOVE_REMOVED and "bob" not in fake_r2.objects[KEY].decode())
    r = await config.remove_whitelist_user("dearella")
    check("D14 del管理员被拒（显式拒绝而非不存在）", r == config.REMOVE_ADMIN_REJECTED, r)
    r = await config.remove_whitelist_user("ghost")
    check("D15 del不存在=missing", r == config.REMOVE_MISSING, r)

    # ---- D4 重启模拟：内存清空 -> 从 R2 拉取；被删用户不复活 ----
    config.WHITELIST_USERS.clear()
    await config.load_whitelist()
    check("D16 重启后从R2恢复且无复活", config.WHITELIST_USERS == {"alice", "carol", "dave", "112233"}, str(config.WHITELIST_USERS))

    # ---- D5 R2 权威：本地残留数据被 R2 覆盖 ----
    local_file().write_text("localonly\n", encoding="utf-8")
    config.WHITELIST_USERS.clear()
    await config.load_whitelist()
    check("D17 R2优先于本地", config.WHITELIST_USERS == {"alice", "carol", "dave", "112233"} and "localonly" not in config.WHITELIST_USERS)
    check("D18 本地缓存被回写为R2内容", "localonly" not in local_file().read_text(encoding="utf-8"))

    # ---- D6 播种迁移：R2 对象不存在 + 本地有数据 -> 推送种子 ----
    reset_all(r2_mode=True)
    local_file().parent.mkdir(parents=True, exist_ok=True)
    local_file().write_text("seed1\nseed2\n", encoding="utf-8")
    await config.load_whitelist()
    check("D19 本地回退加载", config.WHITELIST_USERS == {"seed1", "seed2"})
    check("D20 播种推送到R2", fake_r2.objects.get(KEY, b"").decode().split() == ["seed1", "seed2"], str(fake_r2.objects.get(KEY)))

    # R2 空且本地也空 -> 不播种
    reset_all(r2_mode=True)
    uploads_before = fake_r2.uploads
    await config.load_whitelist()
    check("D21 双空不播种", fake_r2.uploads == uploads_before and config.WHITELIST_USERS == set())

    # ---- D7 网络故障回退 ----
    reset_all(r2_mode=True)
    fake_r2.objects[KEY] = b"remoteuser\n"
    local_file().parent.mkdir(parents=True, exist_ok=True)
    local_file().write_text("localuser\n", encoding="utf-8")
    fake_r2.fail_download = True
    await config.load_whitelist()
    check("D22 下载故障回退本地不清空", config.WHITELIST_USERS == {"localuser"})

    # 对象存在但读取失败（exists=True）-> 不播种，防止本地旧数据覆盖远端
    fake_r2.fail_download = True
    fake_r2.exists_answer = True
    uploads_before = fake_r2.uploads
    await config.load_whitelist()
    check("D23 对象存在但不可读：不播种", fake_r2.uploads == uploads_before)
    fake_r2.exists_answer = None
    fake_r2.fail_download = False

    # ---- D8 推送失败：本地仍生效 + 自愈 ----
    reset_all(r2_mode=True)
    fake_r2.objects[KEY] = b"alice\n"
    await config.load_whitelist()
    fake_r2.fail_upload = True
    r = await config.add_whitelist_user("eric")
    check("D24 推送失败返回added_sync_failed", r == config.ADD_SYNC_FAILED, r)
    check("D25 本地文件仍写入", "eric" in local_file().read_text(encoding="utf-8"))
    check("D26 R2暂无eric", "eric" not in fake_r2.objects[KEY].decode())
    fake_r2.fail_upload = False
    r = await config.add_whitelist_user("frank")
    check("D27 恢复后add成功", r == config.ADD_ADDED, r)
    r2_users = fake_r2.objects[KEY].decode().split()
    check("D28 全量推送自愈（eric补齐）", "eric" in r2_users and "frank" in r2_users, str(r2_users))

    # 推送失败的删除：本地已移除，R2 待自愈
    fake_r2.fail_upload = True
    r = await config.remove_whitelist_user("eric")
    check("D29 删除推送失败状态", r == config.REMOVE_SYNC_FAILED, r)
    fake_r2.fail_upload = False
    await config.add_whitelist_user("temporary_push")
    check("D30 删除也随全量推送自愈", "eric" not in fake_r2.objects[KEY].decode())

    # ---- D9 并发增删一致性（R2 模式）----
    reset_all(r2_mode=True)
    await config.load_whitelist()  # 空
    await asyncio.gather(
        config.add_whitelist_user("g1"),
        config.add_whitelist_user("g2"),
        config.add_whitelist_user("g3"),
        config.remove_whitelist_user("g2"),
    )
    r2_users = fake_r2.objects[KEY].decode().split()
    check("D31 并发后内存==R2==本地",
          config.WHITELIST_USERS == {"g1", "g3"} and r2_users == ["g1", "g3"]
          and local_file().read_text(encoding="utf-8").split() == ["g1", "g3"],
          f"mem={sorted(config.WHITELIST_USERS)} r2={r2_users}")

    # 并发 load 与 add 交叉：最终状态仍一致
    await asyncio.gather(
        config.load_whitelist(),
        config.add_whitelist_user("g4"),
        config.remove_whitelist_user("g1"),
    )
    r2_users = fake_r2.objects[KEY].decode().split()
    check("D32 load/add并发后一致", sorted(config.WHITELIST_USERS) == r2_users == local_file().read_text(encoding="utf-8").split(),
          f"mem={sorted(config.WHITELIST_USERS)} r2={r2_users}")


async def t_e_permission_edges():
    section("E. 授权边界与历史 bug 回归")
    reset_all(r2_mode=True)
    fake_r2.objects[KEY] = b"alice\n112233\n"
    await config.load_whitelist()

    # 管理员始终授权（is_admin 优先于白名单），即使不在白名单
    check("E1 管理员授权不依赖白名单", config.is_admin_identity("dearella") is True)
    check("E2 管理员不在用户白名单", config.is_whitelisted_identity("dearella") is False)

    # 白名单用户授权
    check("E3 白名单用户授权", config.is_whitelisted_identity("ALICE") is True)
    check("E4 非白名单拒绝", config.is_whitelisted_identity("mallory") is False)

    # 手改 R2 把管理员塞进文件 -> 加载时被剔除
    fake_r2.objects[KEY] = b"alice\ndearella\nDEARELLA\n"
    config.WHITELIST_USERS.clear()
    await config.load_whitelist()
    check("E5 手改R2注入管理员被过滤", config.WHITELIST_USERS == {"alice"}, str(config.WHITELIST_USERS))

    # 历史 bug 回归：from-import 引用在 load 后仍指向同一 set 对象
    ref = config.WHITELIST_USERS  # 模拟 app.py 的 from config import WHITELIST_USERS
    await config.load_whitelist()
    check("E6 内存set引用恒定（原地更新）", ref is config.WHITELIST_USERS)
    check("E7 引用内容同步", ref == {"alice"})

    # 原子写：.tmp 文件不残留
    check("E8 无tmp残留", not Path(str(local_file()) + ".tmp").exists())

    # /adduser 空目标防御（app 入口已拦，这里测 config 层不崩溃）
    r = await config.add_whitelist_user("   ")
    check("E9 空目标add不崩溃", r in {config.ADD_EXISTS, config.ADD_ADDED}, r)
    r = await config.remove_whitelist_user("@@")
    check("E10 空目标del不崩溃", r == config.REMOVE_MISSING, r)


async def t_f_r2key_guard():
    section("F. R2 key 环境变量安全防护（子进程验证）")
    code = (
        "import sys; sys.path.insert(0, {src!r});"
        "from apitelegramchat import config;"
        "print(config.WHITELIST_R2_KEY)"
    ).format(src=str(_PROJECT_ROOT / "src"))
    for env_key, expect in [
        ("../evil", "config/whitelist.txt"),          # 路径穿越 -> 回退默认
        ("", "config/whitelist.txt"),                  # 空 -> 回退默认
        ("  custom/wl.txt  ", "custom/wl.txt"),        # 正常自定义（含空白修剪）
        ("/leading/wl.txt", "leading/wl.txt"),         # 前导斜杠修剪
    ]:
        env = dict(os.environ)
        env["APITELEGRAMCHAT_WHITELIST_R2_KEY"] = env_key
        env.pop("APITELEGRAMCHAT_DATA_DIR", None)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        got = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else f"<stderr: {out.stderr[-200:]}>"
        check(f"F {env_key!r} -> {expect}", got == expect, f"got={got}")


async def t_g_real_local_fallback():
    section("G. 真实 s3_utils 本地回退（不打补丁，R2 未配置）")
    reset_all(r2_mode=False)
    # 只关掉 R2 配置开关，IO 用真实实现：push 应落 r2_cache 本地镜像
    s3_utils.upload_bytes_to_r2 = REAL_UPLOAD
    s3_utils.download_from_r2 = REAL_DOWNLOAD
    s3_utils.file_exists_in_r2 = REAL_EXISTS
    s3_utils.is_r2_configured = lambda: False

    r = await config.add_whitelist_user("localmirror")
    check("G1 本地模式 add 成功", r == config.ADD_ADDED, r)
    mirror = _TEST_ROOT / "data" / "r2_cache" / KEY
    check("G2 r2_cache 本地镜像已写入", mirror.exists() and mirror.read_text(encoding="utf-8").split() == ["localmirror"],
          str(mirror))
    check("G3 本地白名单文件一致", local_file().read_text(encoding="utf-8").split() == ["localmirror"])
    # 真实 download（本地回退）能读回镜像
    back = await REAL_DOWNLOAD(KEY)
    check("G4 真实download读回镜像", back is not None and back.decode().split() == ["localmirror"])


async def main():
    await t_a_normalization()
    await t_b_parse()
    await t_c_local_mode()
    await t_d_r2_mode()
    await t_e_permission_edges()
    await t_f_r2key_guard()
    await t_g_real_local_fallback()

    print("\n" + "=" * 60)
    print(f" 结果: {PASS} 通过, {FAIL} 失败")
    if _failures:
        print(" 失败项：")
        for f in _failures:
            print(f"   - {f}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
