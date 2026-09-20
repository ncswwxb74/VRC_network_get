"""VRChat 视频请求探针服务器。

提供 media/ 目录下文件的视频直链，完整记录每个连接与请求的细节到 JSONL 日志，
用于对比 VRChat 内 Unity / AVPro 播放器与浏览器的请求行为差异。

支持三种能力：
1. 直链模式：GET /<file> 直接serve文件（HTTP 基线）。
2. TLS：--tls-port 直接终止 TLS（读 Caddy 签好的证书），记录握手细节。
3. Fallback：/fb/<name> 302 到带 token 的 /t/<token>/<file>，对无法解码 H265
   的客户端自动降级到 H264（详见 media/fallback.json 与下方 note_attempt_and_decide）。

用法:
    python server.py [--port 8080] [--host 0.0.0.0] [--media-dir media]
    python server.py --tls-port 8443 --tls-cert cert.pem --tls-key key.pem
"""

import argparse
import itertools
import json
import secrets
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

# 视频相关 MIME。WMF/AVPro 对 Content-Type 敏感，这里必须给准确值。
MIME_MAP = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".m4s": "video/iso.segment",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".ogv": "video/ogg",
    ".wav": "audio/wav",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
DEFAULT_MIME = "application/octet-stream"
CHUNK_SIZE = 64 * 1024

CONN_COUNTER = itertools.count(1)
LOG_LOCK = threading.Lock()
LOG_FH = None  # main() 中打开
MEDIA_ROOT: Path = None

# ---- Fallback 状态（有序变体链：AV1 → VP9 → H265 → H264 …）----
FALLBACK_LOCK = threading.Lock()
# name -> {"variants": [文件名, ...]}；index 0 = 首选(最省流量/最难解码)，末尾 = 最兼容兜底
FALLBACK_MANIFEST = {}
TOKENS = {}              # token -> {name, level, file, ip, created}
# ip -> {level(当前档位索引), best_bytes(本档单次最大传输), last_player_ts, attempts}
IP_STATE = {}
# 单次响应超过这个字节数，才算"真的在播"（而不是启动期的探测/爬行重连）。
# 日志显示 WMF 成功播放会持续传输数百 MB，失败则只在几 MB 内反复重连。
SUSTAINED_THRESHOLD = 20 * 1024 * 1024
# 距上次播放器尝试超过这个间隔、又从头(bytes=0-)开始的请求，判定为"重试"。
# VRChat 视频加载失败后有约 5s 冷却再重试；成功播放不会从头重来。
RETRY_MIN_GAP = 3.0

# TLS 连接的 SNI 无法从 SSLSocket 直接读，用 SNI 回调按 id(sock) 暂存
SNI_BY_CONN = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def is_player_ua(ua: str) -> bool:
    """区分播放器本体请求与 VRChat 的 yt-dlp 解析探测（后者伪装成 Chrome）。"""
    return ("NSPlayer" in ua) or ("WMFSDK" in ua) or ("UnityPlayer" in ua) or ("Unity" in ua)


def load_manifest():
    """载入 media/fallback.json（缺失或损坏则为空，/fb 端点会回 404）。

    支持两种写法，统一归一化为有序 variants 列表：
      {"demo": {"variants": ["demo_av1.mp4", "demo_vp9.webm", "demo_h264.mp4"]}}
      {"demo": {"primary": "a.mkv", "fallback": "b.mp4"}}   # 旧两档格式，兼容
    """
    path = MEDIA_ROOT / "fallback.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[warn] 解析 {path} 失败: {exc}")
        return
    if not isinstance(data, dict):
        return
    for name, spec in data.items():
        if not isinstance(spec, dict):
            continue
        variants = None
        if isinstance(spec.get("variants"), list) and spec["variants"]:
            variants = [str(v) for v in spec["variants"]]
        elif spec.get("primary"):
            variants = [spec["primary"]]
            if spec.get("fallback"):
                variants.append(spec["fallback"])
        if variants:
            FALLBACK_MANIFEST[name] = {"variants": variants}


def _new_ip_state():
    return {"level": 0, "best_bytes": 0, "last_player_ts": None, "attempts": 0}


def mint_token(name, level, filename, ip):
    token = secrets.token_urlsafe(9)
    with FALLBACK_LOCK:
        TOKENS[token] = {"name": name, "level": level, "file": filename,
                         "ip": ip, "created": now_iso()}
    return token


def note_attempt_and_decide(ip, name, is_player, is_new_attempt, now):
    """记录一次播放器尝试，并决定这个 IP 现在该拿变体链里的哪一档。

    降级判据：同一 IP 的播放器"从头再来"(bytes=0-)且距上次尝试已过 RETRY_MIN_GAP，
    而它在当前档从未有过一次超过 SUSTAINED_THRESHOLD 的传输——即"回来重试却没真正播起来"。
    满足则往下退一档（换更兼容的编码），直到最后一档兜底。
    这样能把「解码失败后的重试」与「成功播放前正常的启动期爬行重连」区分开。

    返回 (level, filename, stepped_down)。
    """
    with FALLBACK_LOCK:
        man = FALLBACK_MANIFEST.get(name)
        variants = man["variants"] if man else []
        last_index = len(variants) - 1
        st = IP_STATE.setdefault(ip, _new_ip_state())
        stepped = False
        if is_player and is_new_attempt:
            if (st["last_player_ts"] is not None
                    and now - st["last_player_ts"] > RETRY_MIN_GAP
                    and st["best_bytes"] < SUSTAINED_THRESHOLD
                    and st["level"] < last_index):
                st["level"] += 1
                st["best_bytes"] = 0      # 换了一档，重新判断这档能否播起来
                stepped = True
            st["attempts"] += 1
            st["last_player_ts"] = now
        level = min(st["level"], last_index) if variants else 0
        filename = variants[level] if variants else None
        return level, filename, stepped


def note_bytes(ip, bytes_sent):
    with FALLBACK_LOCK:
        st = IP_STATE.setdefault(ip, _new_ip_state())
        if bytes_sent > st["best_bytes"]:
            st["best_bytes"] = bytes_sent


def log_event(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with LOG_LOCK:
        LOG_FH.write(line + "\n")
        LOG_FH.flush()


def parse_range(spec: str, size: int):
    """解析单个 Range 头。

    返回 (start, end, multi_truncated) / "unsatisfiable" / None(格式非法，按 RFC 忽略、回 200)。
    """
    if not spec.startswith("bytes="):
        return None
    parts = [p.strip() for p in spec[len("bytes="):].split(",")]
    multi = len(parts) > 1  # 多 range 只取第一个，日志中标记
    part = parts[0]
    if "-" not in part:
        return None
    first, last = part.split("-", 1)
    try:
        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix == 0 or size == 0:
                return "unsatisfiable"
            return (max(0, size - suffix), size - 1, multi)
        start = int(first)
        if start >= size:
            return "unsatisfiable"
        end = int(last) if last else size - 1
        if end < start:
            return None
        return (start, min(end, size - 1), multi)
    except ValueError:
        return None


def get_lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


class ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 开 keep-alive，才能观察连接复用
    server_version = "VRCProbe/0.1"
    timeout = 60

    # ---- 连接生命周期 ----

    def setup(self):
        super().setup()
        self.conn_id = next(CONN_COUNTER)
        self.conn_req = 0
        record = {
            "event": "connection_open", "ts": now_iso(), "conn": self.conn_id,
            "client_ip": self.client_address[0], "client_port": self.client_address[1],
        }
        tls = self._tls_info()
        if tls:
            record["tls"] = tls
        log_event(record)
        tls_note = f" TLS={tls['version']}/{tls.get('cipher')}" if tls else ""
        print(f"[conn#{self.conn_id}] open   "
              f"{self.client_address[0]}:{self.client_address[1]}{tls_note}")

    def _tls_info(self):
        """握手完成后从 SSLSocket 读协商结果；非 TLS 连接返回 None。"""
        conn = self.connection
        if not isinstance(conn, ssl.SSLSocket):
            return None
        try:
            cipher = conn.cipher()
            return {
                "version": conn.version(),
                "cipher": cipher[0] if cipher else None,
                "alpn": conn.selected_alpn_protocol(),
                "sni": SNI_BY_CONN.pop(id(conn), None),
            }
        except (ssl.SSLError, OSError):
            return {"error": "introspect_failed"}

    def finish(self):
        log_event({
            "event": "connection_close", "ts": now_iso(), "conn": self.conn_id,
            "requests_served": self.conn_req,
        })
        print(f"[conn#{self.conn_id}] close  ({self.conn_req} requests)")
        try:
            super().finish()
        except OSError:
            pass

    # ---- 默认 stderr 日志换成自己的记录 ----

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        # 捕获解析失败/不支持的请求（畸形请求行等），这些对分析同样重要
        log_event({
            "event": "protocol_error", "ts": now_iso(),
            "conn": getattr(self, "conn_id", None),
            "detail": fmt % args,
            "request_line": getattr(self, "requestline", ""),
        })

    # ---- 请求入口 ----

    def do_GET(self):
        self._handle(include_body=True)

    def do_HEAD(self):
        self._handle(include_body=False)

    def do_OPTIONS(self):
        self._handle(include_body=False, method_allowed=False)

    def do_POST(self):
        self._handle(include_body=False, method_allowed=False)

    def _handle(self, include_body: bool, method_allowed: bool = True):
        self.conn_req += 1
        started = time.perf_counter()
        url = urlsplit(self.path)
        record = {
            "event": "request", "ts": now_iso(),
            "conn": self.conn_id, "conn_req": self.conn_req,
            "client_ip": self.client_address[0], "client_port": self.client_address[1],
            "request_line": self.requestline,
            "method": self.command, "path": self.path, "http_version": self.request_version,
            "headers": list(self.headers.items()),  # 保留原始大小写与顺序
        }
        if url.query:
            # 查询串不参与文件定位，专用于区分测试者（如 ?tag=alice）
            record["query"] = url.query
        # 先记一条 request_start：若响应长时间流式传输中服务器被停掉，
        # 至少保住请求行与请求头
        log_event({**record, "event": "request_start"})
        resp = {}
        try:
            if not method_allowed:
                self._send_simple(405, b"method not allowed\n", resp)
            else:
                self._route(url, include_body, record, resp)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, OSError) as exc:
            resp["client_aborted"] = True
            resp["abort_error"] = type(exc).__name__
            self.close_connection = True
        record["response"] = resp
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        log_event(record)
        rng = self.headers.get("Range")
        rng_part = f' Range="{rng}"' if rng else ""
        ua = self.headers.get("User-Agent", "-")
        print(f"[conn#{self.conn_id} req#{self.conn_req}] {self.client_address[0]} "
              f"{self.command} {self.path}{rng_part} -> {resp.get('status', '?')} "
              f"sent={resp.get('bytes_sent', 0)} UA={ua}")

    # ---- 路由与响应 ----

    def _route(self, url, include_body, record, resp):
        rel = unquote(url.path)
        if rel in ("", "/"):
            self._serve_index(include_body, resp)
            return
        if rel.startswith("/fb/"):
            self._handle_fallback_base(rel[len("/fb/"):].strip("/"), record, resp)
            return
        if rel.startswith("/t/"):
            parts = rel[len("/t/"):].split("/", 1)
            token = parts[0]
            self._handle_token(token, include_body, record, resp)
            return
        target = (MEDIA_ROOT / rel.lstrip("/")).resolve()
        if not target.is_relative_to(MEDIA_ROOT) or not target.is_file():
            record["file"] = None
            self._send_simple(404, b"not found\n", resp)
            return
        record["file"] = target.relative_to(MEDIA_ROOT).as_posix()
        self._serve_file(target, include_body, resp)

    # ---- Fallback：token 跳转 + H265→H264 降级 ----

    def _new_attempt(self):
        """判断该请求是否是"一次播放的开头"（从头拉或无 Range），
        用以和 Cues 索引读取 / 启动期爬行重连区分。"""
        rng = self.headers.get("Range", "")
        return rng == "" or rng.startswith("bytes=0-")

    def _send_redirect(self, location, resp, extra=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        resp.update({"status": 302, "bytes_sent": 0, "location": location})
        if extra:
            resp.update(extra)

    def _handle_fallback_base(self, name, record, resp):
        """世界里填的入口 URL：/fb/<name> → 302 到带 token 的变体地址。"""
        if name not in FALLBACK_MANIFEST:
            record["file"] = None
            self._send_simple(404, b"unknown fallback set\n", resp)
            return
        ua = self.headers.get("User-Agent", "")
        is_player = is_player_ua(ua)
        level, filename, stepped = note_attempt_and_decide(
            self.client_address[0], name, is_player, self._new_attempt(), time.time())
        if not filename:
            self._send_simple(404, b"variant file missing\n", resp)
            return
        token = mint_token(name, level, filename, self.client_address[0])
        location = f"/t/{token}/{quote(filename)}"
        record["fallback"] = {"via": "base", "name": name, "level": level,
                              "file": filename, "token": token,
                              "is_player": is_player, "stepped_down": stepped}
        self._send_redirect(location, resp, {"level": level})

    def _handle_token(self, token, include_body, record, resp):
        """token 化的实际取片地址；若该 IP 已退到更兼容的档，而 token 仍指旧档，再跳一次。"""
        info = TOKENS.get(token)
        if not info:
            record["file"] = None
            self._send_simple(404, b"unknown or expired token\n", resp)
            return
        name = info["name"]
        ua = self.headers.get("User-Agent", "")
        is_player = is_player_ua(ua)
        level, filename, stepped = note_attempt_and_decide(
            self.client_address[0], name, is_player, self._new_attempt(), time.time())
        record["fallback"] = {"via": "token", "name": name, "token": token,
                              "token_level": info["level"], "decided_level": level,
                              "file": filename, "is_player": is_player,
                              "stepped_down": stepped}
        # 该 IP 已退到更兼容的档，但当前 token 指的是旧档 → 再签一个新档 token 跳过去
        if filename and level > info["level"]:
            new_token = mint_token(name, level, filename, self.client_address[0])
            location = f"/t/{new_token}/{quote(filename)}"
            record["fallback"]["redirect_to"] = location
            self._send_redirect(location, resp, {"level": level})
            return
        target = (MEDIA_ROOT / info["file"]).resolve()
        if not target.is_relative_to(MEDIA_ROOT) or not target.is_file():
            record["file"] = None
            self._send_simple(404, b"file missing\n", resp)
            return
        record["file"] = target.relative_to(MEDIA_ROOT).as_posix()
        # 用 try/finally：成功播放常以客户端主动断开收尾（_serve_file 抛异常），
        # 仍要把已传字节记进 best_bytes，否则后续重试会被误判为"从没播起来"而错误降级
        try:
            self._serve_file(target, include_body, resp)
        finally:
            note_bytes(self.client_address[0], resp.get("bytes_sent", 0))

    def _send_simple(self, status, body: bytes, resp, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        bytes_sent = 0
        if self.command != "HEAD" and body:
            self.wfile.write(body)
            bytes_sent = len(body)
        resp.update({"status": status, "bytes_sent": bytes_sent})

    def _serve_index(self, include_body, resp):
        files = sorted(p for p in MEDIA_ROOT.rglob("*") if p.is_file())
        rows = []
        for p in files:
            rel = p.relative_to(MEDIA_ROOT).as_posix()
            href = quote(rel)
            rows.append(f'<tr><td><a href="/{href}">/{rel}</a></td>'
                        f"<td>{p.stat().st_size:,}</td></tr>")
        ips = ", ".join(get_lan_ips()) or "?"
        body = (
            "<!doctype html><meta charset='utf-8'><title>VRC probe</title>"
            f"<h1>VRC 视频探针服务器</h1><p>本机地址: {ips}</p>"
            "<table border='1' cellpadding='4'><tr><th>文件</th><th>字节</th></tr>"
            + "".join(rows) + "</table>"
        ).encode("utf-8")
        self._send_simple(200, body, resp, content_type="text/html; charset=utf-8")

    def _serve_file(self, target: Path, include_body, resp):
        st = target.stat()
        size = st.st_size
        suffix = target.suffix.lower()
        ctype = MIME_MAP.get(suffix, DEFAULT_MIME)

        status, start, end = 200, 0, size - 1
        range_header = self.headers.get("Range")
        if range_header:
            parsed = parse_range(range_header, size)
            if parsed == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                resp.update({"status": 416, "bytes_sent": 0})
                return
            if parsed is not None:
                start, end, multi = parsed
                status = 206
                if multi:
                    resp["multi_range_truncated"] = True

        length = max(0, end - start + 1)
        # 提前填入响应信息并在传输中滚动更新 bytes_sent，
        # 这样客户端中途断开时也能知道实际发了多少
        resp.update({"status": status, "content_type": ctype,
                     "content_length": length, "bytes_sent": 0})
        if status == 206:
            resp["content_range"] = f"bytes {start}-{end}/{size}"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        if suffix == ".m3u8":
            # 播放列表可能被反复拉取，禁止缓存以便观察每次请求
            self.send_header("Cache-Control", "no-store")
        self.end_headers()

        bytes_sent = 0
        if include_body and length:
            with open(target, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_sent += len(chunk)
                    resp["bytes_sent"] = bytes_sent
                    remaining -= len(chunk)


def build_tls_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    ctx.set_alpn_protocols(["http/1.1"])  # 只做 HTTP/1.1

    def sni_cb(sslobj, server_name, context):
        # SSLSocket 没有直接读 SNI 的公开 API，用回调按 id 暂存，setup() 里取回
        if server_name:
            SNI_BY_CONN[id(sslobj)] = server_name

    ctx.sni_callback = sni_cb
    return ctx


class TLSHTTPServer(ThreadingHTTPServer):
    """在 accept 后用 SSLContext 包一层，直接终止 TLS（不经 Caddy 反代，
    从而保住游戏侧请求头的原始大小写/顺序，并能记录握手细节）。"""

    def __init__(self, addr, handler, ssl_context):
        super().__init__(addr, handler)
        self.ssl_context = ssl_context

    def get_request(self):
        sock, addr = super().get_request()
        return self.ssl_context.wrap_socket(sock, server_side=True), addr

    def handle_error(self, request, client_address):
        # TLS 握手失败（明文/扫描/SNI 不符）很常见，不打印堆栈刷屏
        pass


def _serve(server, scheme, host, port):
    print(f"监听 : {scheme}://{host}:{port}/")
    try:
        server.serve_forever()
    except Exception as exc:  # noqa: BLE001 线程里出错也要落日志
        log_event({"event": "server_error", "ts": now_iso(),
                   "scheme": scheme, "port": port, "error": repr(exc)})


def main():
    global LOG_FH, MEDIA_ROOT
    parser = argparse.ArgumentParser(description="VRChat 视频请求探针服务器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP 端口；设为 0 关闭 HTTP")
    parser.add_argument("--tls-port", type=int, default=0,
                        help="HTTPS 端口（0=关闭）；需配合 --tls-cert/--tls-key")
    parser.add_argument("--tls-cert", help="证书链 PEM（可直接指向 Caddy 签好的 .crt）")
    parser.add_argument("--tls-key", help="私钥 PEM（Caddy 的 .key）")
    parser.add_argument("--media-dir", default=str(Path(__file__).parent / "media"))
    parser.add_argument("--log-dir", default=str(Path(__file__).parent / "logs"))
    args = parser.parse_args()

    MEDIA_ROOT = Path(args.media_dir).resolve()
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    load_manifest()
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    LOG_FH = open(log_path, "a", encoding="utf-8")
    log_event({"event": "server_start", "ts": now_iso(), "host": args.host,
               "http_port": args.port, "tls_port": args.tls_port,
               "media_dir": str(MEDIA_ROOT),
               "fallback_sets": sorted(FALLBACK_MANIFEST)})

    servers = []
    if args.port:
        http_srv = ThreadingHTTPServer((args.host, args.port), ProbeHandler)
        http_srv.daemon_threads = True
        servers.append((http_srv, "http", args.port))
    if args.tls_port:
        if not (args.tls_cert and args.tls_key):
            parser.error("--tls-port 需要同时提供 --tls-cert 和 --tls-key")
        ctx = build_tls_context(args.tls_cert, args.tls_key)
        tls_srv = TLSHTTPServer((args.host, args.tls_port), ProbeHandler, ctx)
        tls_srv.daemon_threads = True
        servers.append((tls_srv, "https", args.tls_port))
    if not servers:
        parser.error("HTTP 与 HTTPS 都被关闭了，没有可监听的端口")

    print(f"媒体目录 : {MEDIA_ROOT}")
    print(f"日志文件 : {log_path}")
    if FALLBACK_MANIFEST:
        print(f"Fallback : {', '.join(sorted(FALLBACK_MANIFEST))}")
    for ip in get_lan_ips():
        print(f"  本机地址: {ip}")
    print("Ctrl+C 停止。\n")

    # 除最后一个外都放到后台线程，最后一个占用主线程以便 Ctrl+C
    threads = []
    for srv, scheme, port in servers[:-1]:
        t = threading.Thread(target=_serve, args=(srv, scheme, args.host, port),
                             daemon=True)
        t.start()
        threads.append(t)
    last_srv, last_scheme, last_port = servers[-1]
    try:
        _serve(last_srv, last_scheme, args.host, last_port)
    except KeyboardInterrupt:
        pass
    finally:
        log_event({"event": "server_stop", "ts": now_iso()})
        LOG_FH.close()
        for srv, _, _ in servers:
            srv.server_close()


if __name__ == "__main__":
    main()
