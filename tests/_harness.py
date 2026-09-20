"""测试公共件：在线程里起一个 server.py 的 ProbeHandler 实例，用 http.client 直接驱动。

不依赖第三方包。每个测试用例自己控制 IP_STATE / TOKENS，并通过回拨
IP_STATE[ip]["last_player_ts"] 来模拟时间间隔，避免真的 sleep。
"""
import http.client
import io
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

# server.py 每个连接/请求都往 stdout 打一行，测试里只会淹没 unittest 的汇总，直接屏蔽
server.print = lambda *a, **k: None  # noqa: E731

PLAYER_UA = "NSPlayer/12.00.26100.1234 WMFSDK/12.00.26100.1234"
YTDLP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
LOCAL_IP = "127.0.0.1"


class ProbeFixture:
    """一次性搭好媒体目录 + 变体链清单 + 日志句柄 + 监听 127.0.0.1 随机端口。"""

    def __init__(self, variants=("v_av1.mp4", "v_vp9.webm", "v_h264.mp4"),
                 sizes=None, cdn_base=None):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"))
        media = Path(self.tmp.name) / "media"
        media.mkdir()
        self.variants = list(variants)
        sizes = sizes or {}
        for v in self.variants:
            (media / v).write_bytes(b"x" * sizes.get(v, 4096))
        (media / "fallback.json").write_text(
            json.dumps({"demo": {"variants": self.variants}}), encoding="utf-8")

        server.MEDIA_ROOT = media.resolve()
        server.FALLBACK_MANIFEST.clear()
        server.load_manifest()
        server.LOG_FH = io.StringIO()
        server.CDN_BASE = cdn_base
        self.reset_state()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ProbeHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        # poll_interval 调小：shutdown() 要等一轮 poll，默认 0.5s 会让每个用例白等半秒
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @staticmethod
    def reset_state():
        server.IP_STATE.clear()
        server.TOKENS.clear()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.CDN_BASE = None
        self.tmp.cleanup()

    # ---- 驱动 ----

    def request(self, path, ua=PLAYER_UA, rng=None, method="GET"):
        """发一个请求，返回 (status, headers_dict, body)。不跟随重定向。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"User-Agent": ua}
        if rng:
            headers["Range"] = rng
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
        conn.close()
        return out

    def resolve_token_path(self, name="demo", ua=YTDLP_UA):
        """模拟 yt-dlp：打 /fb/<name>，返回 302 指向的 token 路径。"""
        status, headers, _ = self.request(f"/fb/{name}", ua=ua)
        assert status == 302, status
        return headers["location"]

    # ---- 时间操控 ----

    @staticmethod
    def age_last_attempt(seconds, ip=LOCAL_IP):
        """把该 IP 上次尝试的时间戳往回拨，模拟"过了 seconds 秒后又来"。"""
        server.IP_STATE[ip]["last_player_ts"] -= seconds

    @staticmethod
    def state(ip=LOCAL_IP):
        return server.IP_STATE.get(ip)

    @staticmethod
    def events():
        return [json.loads(line) for line in server.LOG_FH.getvalue().splitlines()
                if line.strip()]
