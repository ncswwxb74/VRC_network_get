"""--tls-port 直接终止 TLS 的行为断言：握手细节进日志、坏握手不拖垮服务。

需要 openssl 命令生成自签证书；找不到就整组跳过。
运行：python -m unittest discover -s tests -v
"""
import http.client
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from _harness import PLAYER_UA
import server

OPENSSL = shutil.which("openssl")


def make_selfsigned(dirpath: Path, cn="probe.test"):
    cert, key = dirpath / "cert.pem", dirpath / "key.pem"
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True)
    return cert, key


@unittest.skipUnless(OPENSSL, "需要 openssl 生成自签证书")
class TlsTests(unittest.TestCase):
    CN = "probe.test"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TEMP"))
        root = Path(cls.tmp.name)
        cls.cert, cls.key = make_selfsigned(root, cls.CN)
        media = root / "media"
        media.mkdir()
        (media / "clip.mp4").write_bytes(b"x" * 1024)
        server.MEDIA_ROOT = media.resolve()
        server.FALLBACK_MANIFEST.clear()
        server.CDN_BASE = None

        ctx = server.build_tls_context(str(cls.cert), str(cls.key))
        cls.httpd = server.TLSHTTPServer(("127.0.0.1", 0), server.ProbeHandler, ctx)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        server.LOG_FH = io.StringIO()

    def events(self):
        return [json.loads(line) for line in server.LOG_FH.getvalue().splitlines()
                if line.strip()]

    def client_ctx(self, alpn=None):
        ctx = ssl.create_default_context(cafile=str(self.cert))
        if alpn:
            ctx.set_alpn_protocols(alpn)
        return ctx

    def https_get(self, path, alpn=None, sni=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5,
                                           context=self.client_ctx(alpn))
        if sni:
            # http.client 用 host 作 SNI；要指定别的名字得自己 wrap
            conn.sock = self.client_ctx(alpn).wrap_socket(
                socket.create_connection(("127.0.0.1", self.port), timeout=5),
                server_hostname=sni)
        conn.request("GET", path, headers={"User-Agent": PLAYER_UA})
        resp = conn.getresponse()
        body = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
        conn.close()
        return out

    def test_serves_file_over_tls(self):
        status, headers, body = self.https_get("/clip.mp4", sni=self.CN)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(len(body), 1024)

    def test_connection_open_records_handshake(self):
        self.https_get("/clip.mp4", alpn=["http/1.1"], sni=self.CN)
        opens = [e for e in self.events() if e["event"] == "connection_open"]
        self.assertEqual(len(opens), 1)
        tls = opens[0]["tls"]
        self.assertIn(tls["version"], ("TLSv1.2", "TLSv1.3"))
        self.assertTrue(tls["cipher"])
        self.assertEqual(tls["alpn"], "http/1.1")
        self.assertEqual(tls["sni"], self.CN)

    def test_alpn_absent_when_client_offers_none(self):
        self.https_get("/clip.mp4", sni=self.CN)
        tls = [e for e in self.events() if e["event"] == "connection_open"][0]["tls"]
        self.assertIsNone(tls["alpn"])

    def test_plaintext_on_tls_port_does_not_kill_server(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                s.recv(64)          # 服务器要么回 alert 要么直接关，都行
            except OSError:
                pass
        # 之后正常 TLS 请求仍然通
        status, _, _ = self.https_get("/clip.mp4", sni=self.CN)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
