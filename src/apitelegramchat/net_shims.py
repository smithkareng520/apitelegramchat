# =====================================================================
# net_shims.py — curl / wget 的 Python stdlib 兜底 shim
# =====================================================================
# 背景：项目旧版 Dockerfile（node:22-bookworm-slim）没有安装 curl/wget，
# Landlock 沙箱本身并不拦截网络，模型执行 `curl https://...` 得到的只是
# "command not found"，白白浪费一次工具调用（还得靠模型自己聪明地改用
# python urllib 重试）。
#
# 解决方案（双保险）：
#   1. 新 Dockerfile 已直接安装真 curl / wget / git / jq / zip；
#   2. 对于暂未重建镜像的存量部署，本模块在 bash 会话启动时把纯 stdlib
#      实现的 curl / wget 脚本放进 runtime bin（PATH 第一位）。镜像里
#      一旦出现真二进制，shim 自动让位（shutil.which 检测）。
#
# shim 特性：
#   - 幂等：内容与当前版本不一致才重写（写入 tmp 后 os.replace，原子）；
#   - 自包含：只依赖 python3 标准库，在沙箱环境（无 PYTHONPATH）可运行；
#   - 语义对齐：退出码、-f/--fail、-L 重定向、--compressed、-w 常用变量
#     等与真 curl 对齐；不支持的旗标会以 curl 同款退出码 2 明确报错，
#     让模型知道该换基础写法而不是反复撞墙。
# =====================================================================

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# curl shim 源码（独立脚本，禁止 import 本项目任何模块）
# ----------------------------------------------------------------------
# 注意：shim 文件头的 marker 注释（net-shim-v1）用于版本识别，改版本时同步更新。
_CURL_SHIM_SOURCE = r'''#!/usr/bin/python3
# apitelegramchat curl shim (marker: net-shim-v1)
# Real curl is not installed in this container image; this stdlib-only
# fallback implements the common curl subset so agent workflows keep
# working. Exit codes follow curl conventions.
import argparse
import base64
import gzip
import socket
import ssl
import sys
import time
import zlib

import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

SHIM_ID = "curl-shim/1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_URL_MALFORMED = 3
EXIT_DNS = 6
EXIT_CONNECT = 7
EXIT_HTTP_ERROR = 22
EXIT_WRITE_ERROR = 26
EXIT_TIMEOUT = 28
EXIT_SSL = 60

# 对语义无影响的旗标：接受并忽略（避免无谓的失败）。
_NOOP_FLAGS = {
    "-0", "--http1.0", "--http1.1", "--http2", "--http2-prior-knowledge",
    "--no-buffer", "-N", "--progress-bar", "-#", "-4", "-6",
    "--no-keepalive", "--keepalive", "--tcp-nodelay", "-g", "--globoff",
    "--path-as-is", "--silent-with-error", "--no-progress-meter",
}


def eprint(*args):
    print(*args, file=sys.stderr)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _CountingRedirect(urllib.request.HTTPRedirectHandler):
    count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        type(self).count += 1
        if type(self).count > 50:
            raise urllib.error.HTTPError(
                newurl, code, "curl-shim: too many redirects", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(follow, verify, proxies):
    handlers = []
    if follow:
        handlers.append(_CountingRedirect())
    else:
        handlers.append(_NoRedirect())
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if proxies:
        handlers.append(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


def map_url_error(err):
    reason = getattr(err, "reason", err)
    text = str(reason)
    if isinstance(reason, socket.gaierror) or "name or service" in text.lower() \
            or "temporary failure in name resolution" in text.lower() \
            or "getaddrinfo" in text.lower():
        return EXIT_DNS, "Could not resolve host"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return EXIT_TIMEOUT, "Operation timed out"
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return EXIT_SSL, "SSL certificate problem"
    if isinstance(reason, ConnectionRefusedError):
        return EXIT_CONNECT, "Failed to connect (connection refused)"
    return EXIT_CONNECT, "Failed to connect (%s)" % text


def maybe_decompress(body, headers):
    enc = (headers.get("Content-Encoding") or "").strip().lower()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        pass
    return body


def read_body(resp, deadline):
    chunks = []
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise socket.timeout("max-time exceeded")
        try:
            chunk = resp.read(65536)
        except socket.timeout:
            raise
        except OSError:
            raise
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def render_write_out(fmt, ctx):
    out = []
    i = 0
    n = len(fmt)
    while i < n:
        ch = fmt[i]
        if ch == "\\" and i + 1 < n:
            nxt = fmt[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "t":
                out.append("\t"); i += 2; continue
            if nxt == "r":
                out.append("\r"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
        if ch == "%" and fmt.startswith("%{", i):
            end = fmt.find("}", i + 2)
            if end > 0:
                key = fmt[i + 2:end]
                val = ctx.get(key, "")
                out.append(str(val))
                i = end + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def header_text(resp):
    lines = ["HTTP status: %s %s" % (getattr(resp, "status", None) or resp.getcode(), resp.msg)]
    try:
        for k, v in resp.headers.items():
            lines.append("%s: %s" % (k, v))
    except Exception:
        pass
    return "\n".join(lines) + "\n\n"


def parse_headers(header_args, user_agent):
    headers = {}
    headers["User-Agent"] = user_agent or SHIM_ID
    for raw in header_args:
        if ":" not in raw:
            eprint("curl-shim: malformed -H header (expected 'Name: value'): %s" % raw)
            sys.exit(EXIT_USAGE)
        name, _, value = raw.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if raw.rstrip().endswith(":") and not value:
            headers.pop(name, None)  # "X:" 移除默认头（如删除默认 UA）
        else:
            headers[name] = value
    return headers


def build_data(opts):
    """把 -d/--data/--data-raw/--data-binary/--json 合成请求体。"""
    parts = []
    saw_json = bool(opts.json)
    for item in opts.json or []:
        parts.append(("raw", item.encode("utf-8")))
    for item in opts.data or []:
        if item.startswith("@"):
            try:
                with open(item[1:], "rb") as f:
                    parts.append(("form", f.read()))
            except OSError as exc:
                eprint("curl-shim: could not read data file: %s" % exc)
                sys.exit(EXIT_USAGE)
        else:
            parts.append(("form", item.encode("utf-8")))
    for item in opts.data_raw or []:
        parts.append(("raw", item.encode("utf-8")))
    for item in opts.data_binary or []:
        if item.startswith("@"):
            try:
                with open(item[1:], "rb") as f:
                    parts.append(("raw", f.read()))
            except OSError as exc:
                eprint("curl-shim: could not read data file: %s" % exc)
                sys.exit(EXIT_USAGE)
        else:
            parts.append(("raw", item.encode("utf-8")))
    if not parts:
        return None, saw_json
    # 只有 form 类多段才用 & 连接（curl -d 语义）；raw 类按出现顺序拼接。
    if all(kind == "form" for kind, _ in parts) and len(parts) > 1:
        return b"&".join(payload for _, payload in parts), saw_json
    return b"".join(payload for _, payload in parts), saw_json


def append_query(url, data):
    if not data:
        return url
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    encoded = "&".join(
        "%s=%s" % (quote(p, safe=""), quote(v, safe=""))
        for p, _, v in (piece.partition("=") for piece in data.split("&"))
    )
    sep = "&" if "?" in url else "?"
    return url + sep + encoded


def transfer_one(url, opts, method, headers, data, opener):
    start = time.monotonic()
    deadline = (start + opts.max_time) if opts.max_time else None
    timeout = opts.max_time or opts.connect_timeout or 60.0
    # 重定向计数是类属性；不重置会跨 URL 与 --retry 累积，导致
    # 多 URL 场景误报 "too many redirects"、-w %{num_redirects} 报累计值。
    _CountingRedirect.count = 0

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    status = 0
    effective_url = url
    num_redirects = 0
    body = b""
    resp_headers = {}
    content_type = ""

    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        resp = err
    except urllib.error.URLError as err:
        code, message = map_url_error(err)
        eprint("curl-shim: (%d) %s: %s" % (code, message, url))
        return code
    except socket.timeout:
        eprint("curl-shim: (%d) Operation timed out: %s" % (EXIT_TIMEOUT, url))
        return EXIT_TIMEOUT

    try:
        effective_url = resp.geturl() or url
        status = resp.getcode() or 0
        resp_headers = resp.headers
        content_type = resp_headers.get("Content-Type") or ""
        if not opts.head:
            body = read_body(resp, deadline)
    except socket.timeout:
        eprint("curl-shim: (%d) Operation timed out: %s" % (EXIT_TIMEOUT, url))
        return EXIT_TIMEOUT
    except urllib.error.HTTPError as err:
        status = err.code
        resp_headers = err.headers
        try:
            body = err.read()
        except Exception:
            body = b""
    except OSError as err:
        code, message = map_url_error(err)
        eprint("curl-shim: (%d) %s: %s" % (code, message, url))
        return code
    finally:
        num_redirects = getattr(_CountingRedirect, "count", 0)
        try:
            resp.close()
        except Exception:
            pass

    if opts.compressed:
        body = maybe_decompress(body, resp_headers)

    # ---- 输出 ----
    rc = EXIT_OK
    fail_with_body = bool(getattr(opts, "fail_with_body", False))
    if status >= 400 and opts.fail and not fail_with_body:
        # 普通 --fail：不输出 body，直接以 22 退出。
        eprint("curl-shim: (22) The requested URL returned error: %d" % status)
        return EXIT_HTTP_ERROR
    if status >= 400 and opts.fail and fail_with_body:
        # 真 curl 的 --fail-with-body 语义：输出响应体，然后仍以 22 退出。
        eprint("curl-shim: (22) The requested URL returned error: %d" % status)
        rc = EXIT_HTTP_ERROR
    if opts.head and rc == EXIT_OK:
        sys.stdout.write(header_text(resp))
    else:
        if opts.include:
            sys.stdout.write(header_text(resp))
        if opts.output == "-":
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()
        elif opts.output:
            try:
                with open(opts.output, "wb") as f:
                    f.write(body)
            except OSError as exc:
                eprint("curl-shim: (%d) failed to write %s: %s" % (EXIT_WRITE_ERROR, opts.output, exc))
                return EXIT_WRITE_ERROR
        elif opts.remote_name:
            path = urlsplit(url).path.rstrip("/")
            name = path.rsplit("/", 1)[-1] if path else ""
            if not name:
                eprint("curl-shim: (%d) Remote file name has no length" % EXIT_URL_MALFORMED)
                return EXIT_URL_MALFORMED
            try:
                with open(name, "wb") as f:
                    f.write(body)
                if not (opts.silent and not opts.show_error):
                    eprint("  saved to '%s' (%d bytes)" % (name, len(body)))
            except OSError as exc:
                eprint("curl-shim: (%d) failed to write %s: %s" % (EXIT_WRITE_ERROR, name, exc))
                return EXIT_WRITE_ERROR
        else:
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()

    if opts.write_out:
        ctx = {
            "http_code": status,
            "response_code": status,
            "size_download": len(body),
            "url_effective": effective_url,
            "content_type": content_type,
            "num_redirects": num_redirects,
            "time_total": round(time.monotonic() - start, 3),
            "redirect_url": resp_headers.get("Location") or "",
            "exitcode": rc,
        }
        sys.stdout.write(render_write_out(opts.write_out, ctx))
        sys.stdout.flush()
    return rc


def main(argv):
    parser = argparse.ArgumentParser(
        prog="curl",
        description="curl shim over Python stdlib (real curl is not installed in this image)",
    )
    parser.add_argument("url", nargs="*")
    parser.add_argument("-s", "--silent", action="store_true")
    parser.add_argument("-S", "--show-error", action="store_true")
    parser.add_argument("-L", "--location", action="store_true")
    parser.add_argument("-k", "--insecure", action="store_true")
    parser.add_argument("-f", "--fail", action="store_true")
    parser.add_argument("--fail-with-body", action="store_true")
    parser.add_argument("-i", "--include", action="store_true")
    parser.add_argument("-I", "--head", action="store_true")
    parser.add_argument("-X", "--request", dest="method")
    parser.add_argument("-H", "--header", action="append", default=[])
    parser.add_argument("-d", "--data", action="append", default=[])
    parser.add_argument("--data-raw", action="append", default=[])
    parser.add_argument("--data-binary", action="append", default=[])
    parser.add_argument("--json", action="append", default=[])
    parser.add_argument("-u", "--user")
    parser.add_argument("-A", "--user-agent")
    parser.add_argument("-e", "--referer")
    parser.add_argument("-b", "--cookie")
    parser.add_argument("-m", "--max-time", type=float)
    parser.add_argument("--connect-timeout", type=float)
    parser.add_argument("--compressed", action="store_true")
    parser.add_argument("-w", "--write-out")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--retry", type=int, default=0)
    parser.add_argument("-G", "--get", action="store_true")
    parser.add_argument("-x", "--proxy")
    parser.add_argument("-o", "--output")
    parser.add_argument("-O", "--remote-name", action="store_true")
    parser.add_argument("-V", "--version", action="store_true")
    opts, unknown = parser.parse_known_args(argv)

    if opts.version:
        sys.stdout.write("curl 8.shim (%s)\n" % SHIM_ID)
        return EXIT_OK

    if unknown:
        unsupported = [token for token in unknown if token not in _NOOP_FLAGS]
        if unsupported:
            for token in unsupported:
                eprint(
                    "curl-shim: unsupported option '%s' (this is a stdlib fallback, "
                    "not the real curl). Supported core flags: -s -S -L -k -f -i -I "
                    "-o/-O -X -H -d/--data/--data-raw/--data-binary/--json -u -A -e "
                    "-b -m/--max-time --connect-timeout --compressed -w -G -x --retry. "
                    "Simplify the command or use python3 urllib directly." % token
                )
            return EXIT_USAGE

    urls = [u for u in opts.url if u]
    if not urls:
        eprint("curl-shim: no URL given")
        return EXIT_USAGE
    for u in urls:
        if "://" not in u:
            eprint("curl-shim: (%d) URL rejected: '%s' (expected http/https)" % (EXIT_URL_MALFORMED, u))
            return EXIT_URL_MALFORMED

    if opts.fail_with_body:
        opts.fail = True

    headers = parse_headers(opts.header, opts.user_agent)
    if opts.referer:
        headers["Referer"] = opts.referer
    if opts.cookie and not opts.cookie.startswith("@"):
        headers["Cookie"] = opts.cookie
    if opts.user:
        cred = opts.user if ":" in opts.user else opts.user + ":"
        headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()

    data, saw_json = build_data(opts)
    if saw_json:
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")
    elif data is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if opts.compressed:
        headers.setdefault("Accept-Encoding", "gzip, deflate")

    if opts.get and data is not None:
        urls = [append_query(u, data) for u in urls]
        data = None

    if opts.method:
        method = opts.method.upper()
    elif opts.head:
        method = "HEAD"
    elif data is not None:
        method = "POST"
    else:
        method = "GET"

    proxies = None
    if opts.proxy:
        proxies = {"http": opts.proxy, "https": opts.proxy}

    opener = build_opener(opts.location, opts.insecure, proxies)
    attempts = max(1, (opts.retry or 0) + 1)
    rc = EXIT_OK
    for url in urls:
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2.0, 1.0 * attempt))
            if opts.verbose:
                eprint("* curl-shim %s %s (attempt %d)" % (method, url, attempt + 1))
            rc = transfer_one(url, opts, method, headers, data, opener)
            if rc == EXIT_OK or not opts.retry:
                break
            if rc not in (EXIT_DNS, EXIT_CONNECT, EXIT_TIMEOUT):
                break
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
'''

# ----------------------------------------------------------------------
# wget shim 源码（独立脚本）
# ----------------------------------------------------------------------
_WGET_SHIM_SOURCE = r'''#!/usr/bin/python3
# apitelegramchat wget shim (marker: net-shim-v1)
# Stdlib-only wget fallback used when the image has no real wget.
import argparse
import socket
import ssl
import sys
import time

import urllib.error
import urllib.request
from urllib.parse import urlsplit

SHIM_ID = "Wget.shim (python-stdlib)"

EXIT_OK = 0
EXIT_NET_FAILURE = 4
EXIT_SERVER_ERROR = 8


def eprint(*args):
    print(*args, file=sys.stderr)


class _WgetMaxRedirect(urllib.request.HTTPRedirectHandler):
    """按 --max-redirect 限制重定向次数（默认 20，与 GNU wget 一致）。"""

    def __init__(self, max_redirects):
        super().__init__()
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.max_redirects -= 1
        if self.max_redirects < 0:
            raise urllib.error.HTTPError(
                newurl, code, "wget-shim: too many redirects", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(verify, max_redirects=20):
    handlers = [_WgetMaxRedirect(max(0, int(max_redirects)))]
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def default_filename(url):
    path = urlsplit(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    return name or "index.html"


def fetch(url, opts, opener):
    timeout = opts.timeout or 90.0
    headers = {"User-Agent": opts.user_agent or SHIM_ID}
    for raw in opts.header or []:
        if ":" in raw:
            name, _, value = raw.partition(":")
            headers[name.strip()] = value.strip()
    data = opts.post_data.encode("utf-8") if opts.post_data else None
    if data is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, method="HEAD" if opts.spider else ("POST" if data else "GET"))
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        return err.code, err.headers, b""
    except urllib.error.URLError as err:
        reason = getattr(err, "reason", err)
        text = str(reason)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            eprint("wget-shim: read timeout for %s" % url)
        elif isinstance(reason, socket.gaierror) or "getaddrinfo" in text:
            eprint("wget-shim: unable to resolve host for %s" % url)
        else:
            eprint("wget-shim: network failure for %s (%s)" % (url, text))
        return None, None, b""
    try:
        body = resp.read()
        return resp.getcode() or 200, resp.headers, body
    finally:
        try:
            resp.close()
        except Exception:
            pass


def main(argv):
    parser = argparse.ArgumentParser(
        prog="wget",
        description="wget shim over Python stdlib (real wget is not installed in this image)",
    )
    parser.add_argument("url", nargs="*")
    parser.add_argument("-O", "--output-document")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-nv", "--no-verbose", action="store_true")
    parser.add_argument("-T", "--timeout", type=float)
    parser.add_argument("-t", "--tries", type=int, default=10)
    parser.add_argument("--no-check-certificate", action="store_true")
    parser.add_argument("-U", "--user-agent")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--post-data")
    parser.add_argument("--spider", action="store_true")
    parser.add_argument("--max-redirect", type=int, default=20)
    parser.add_argument("--version", action="store_true")
    opts, unknown = parser.parse_known_args(argv)

    if opts.version:
        sys.stdout.write("GNU Wget shim (python stdlib fallback)\n")
        return EXIT_OK
    if unknown:
        for token in unknown:
            eprint("wget-shim: unsupported option '%s'; supported: -O -q -nv -T -t "
                   "--no-check-certificate -U --header --post-data --spider" % token)
        return 2

    urls = [u for u in opts.url if u]
    if not urls:
        eprint("wget-shim: missing URL")
        return 2
    for u in urls:
        if "://" not in u:
            eprint("wget-shim: invalid URL: %s" % u)
            return 2

    quiet = opts.quiet or opts.no_verbose
    opener = build_opener(not opts.no_check_certificate, opts.max_redirect)
    overall = EXIT_OK
    for url in urls:
        tries = max(1, min(opts.tries, 20))
        status, resp_headers, body = None, None, b""
        for attempt in range(tries):
            if attempt and not quiet:
                eprint("wget-shim: retrying %s (%d/%d)..." % (url, attempt + 1, tries))
            if attempt:
                time.sleep(min(2.0, 1.0 * attempt))
            status, resp_headers, body = fetch(url, opts, opener)
            if status is not None and status < 500:
                break
        if status is None:
            overall = EXIT_NET_FAILURE
            continue
        if status >= 400:
            eprint("wget-shim: ERROR %d: %s" % (status, url))
            overall = EXIT_SERVER_ERROR
            continue
        if opts.spider:
            if not quiet:
                eprint("Remote file exists: %s (status %s)" % (url, status))
            continue
        target = opts.output_document
        if target == "-":
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()
            continue
        if not target:
            target = default_filename(url)
        try:
            with open(target, "wb") as f:
                f.write(body)
        except OSError as exc:
            eprint("wget-shim: cannot write %s: %s" % (target, exc))
            overall = EXIT_NET_FAILURE if overall == EXIT_OK else overall
            continue
        if not quiet:
            size = len(body)
            eprint("Saving to: '%s'" % target)
            eprint("     0K .%s %d bytes" % ("." * min(50, max(1, size // 1024)), size))
    return overall


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
'''


def _shim_is_current(path: Path, source: str) -> bool:
    """目标文件存在且内容与当前版本源码一致时才跳过重写。"""
    try:
        return path.read_text(encoding="utf-8") == source
    except OSError:
        return False


def _find_real_binary(name: str, own_shim: Path) -> str | None:
    """在 PATH 中查找真二进制，排除我们自己安装的 shim。

    关键：`shutil.which("curl")` 可能命中上次安装的 shim 本身（当 PATH
    包含 runtime bin 时，比如单测环境）。若不排除，会导致「检测到二进制
    → 删除 shim → 下次检测不到 → 重装」的抖动。这里用 realpath 比对，
    只有命中路径不是我们的 shim 时才认定镜像里有真二进制。
    """
    found = shutil.which(name)
    if not found:
        return None
    try:
        found_real = os.path.realpath(found)
        shim_real = os.path.realpath(str(own_shim))
    except OSError:
        return found if found != str(own_shim) else None
    if found_real == shim_real:
        return None  # 找到的是我们自己的 shim，不算真二进制
    return found


def ensure_network_shims(runtime_bin: Path) -> None:
    """确保 curl / wget 在沙箱内可用（真实二进制缺失时安装 stdlib shim）。

    - 镜像里已有真二进制（新 Dockerfile 安装了 curl/wget）→ 什么都不做，
      并清掉旧版本 shim 防止 PATH 抢占；
    - runtime bin 中已有同版本 shim → 跳过；
    - 否则原子写入（tmp + os.replace）并 chmod 755。
    任何 IO 失败只记 debug 日志：shim 是尽力而为的增强，绝不能阻断 bash。
    """
    try:
        runtime_bin.mkdir(parents=True, exist_ok=True)
        for name, source in (("curl", _CURL_SHIM_SOURCE), ("wget", _WGET_SHIM_SOURCE)):
            target = runtime_bin / name
            real = _find_real_binary(name, target)
            if real is not None:
                # 镜像自带真二进制，shim 让位；同时清掉旧 shim 防止 PATH 抢占。
                try:
                    if target.exists() or target.is_symlink():
                        target.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if _shim_is_current(target, source):
                continue
            tmp = runtime_bin / f".{name}.shim.{os.getpid()}.tmp"
            tmp.write_text(source, encoding="utf-8")
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
            logger.info("Installed stdlib %s shim at %s (real binary not present in image)", name, target)
    except OSError as exc:
        logger.debug("network shim installation skipped: %s", exc)


__all__ = ["ensure_network_shims"]
