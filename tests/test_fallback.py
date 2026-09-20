"""fallback 链（本地 serve 模式 + CDN 跳转模式）的行为断言。

运行：python -m unittest discover -s tests -v
"""
import unittest

from _harness import LOCAL_IP, PLAYER_UA, YTDLP_UA, ProbeFixture
import server


class LocalModeTests(unittest.TestCase):
    """探针模式（CDN_BASE=None）：本机 serve，字节计数决定是否退档。"""

    def setUp(self):
        self.fx = ProbeFixture()

    def tearDown(self):
        self.fx.close()

    def test_base_redirects_to_token_of_first_variant(self):
        status, headers, _ = self.fx.request("/fb/demo", ua=YTDLP_UA)
        self.assertEqual(status, 302)
        self.assertRegex(headers["location"], r"^/t/[A-Za-z0-9_-]+/v_av1\.mp4$")
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_unknown_set_is_404(self):
        status, _, _ = self.fx.request("/fb/nope", ua=YTDLP_UA)
        self.assertEqual(status, 404)

    def test_token_serves_file_with_video_mime(self):
        path = self.fx.resolve_token_path()
        status, headers, body = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(len(body), 4096)

    def test_token_honours_range(self):
        path = self.fx.resolve_token_path()
        status, headers, body = self.fx.request(path, ua=PLAYER_UA, rng="bytes=100-199")
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 100-199/4096")
        self.assertEqual(len(body), 100)

    def test_retry_without_sustained_transfer_steps_down(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)                 # 第一次尝试（4KB，远小于阈值）
        self.fx.age_last_attempt(server.RETRY_MIN_GAP + 1)  # 过了冷却又从头来 = 重试
        status, headers, _ = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 302)                       # 旧 token → 新档 token
        self.assertRegex(headers["location"], r"^/t/[A-Za-z0-9_-]+/v_vp9\.webm$")
        self.assertEqual(self.fx.state()["level"], 1)
        # 跟过去后能拿到 vp9
        status, headers, _ = self.fx.request(headers["location"], ua=PLAYER_UA)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "video/webm")

    def test_quick_reconnect_within_min_gap_is_not_retry(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        status, _, _ = self.fx.request(path, ua=PLAYER_UA)  # 启动期爬行重连，间隔 ~0s
        self.assertEqual(status, 200)
        self.assertEqual(self.fx.state()["level"], 0)

    def test_sustained_transfer_prevents_step_down(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        server.note_bytes(LOCAL_IP, server.SUSTAINED_THRESHOLD + 1)  # 模拟播了几百 MB
        self.fx.age_last_attempt(server.RETRY_MIN_GAP + 1)
        status, _, _ = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 200)
        self.assertEqual(self.fx.state()["level"], 0)

    def test_local_mode_ignores_retry_max_gap(self):
        """探针模式有字节计数，隔多久重来都按字节判，不受 RETRY_MAX_GAP 限制。"""
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        self.fx.age_last_attempt(server.RETRY_MAX_GAP * 10)
        status, _, _ = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 302)
        self.assertEqual(self.fx.state()["level"], 1)

    def test_non_player_attempts_never_step_down(self):
        path = self.fx.resolve_token_path()
        for _ in range(3):
            self.fx.request(path, ua=YTDLP_UA)
        st = self.fx.state()
        self.assertEqual(st["level"], 0)
        self.assertIsNone(st["last_player_ts"])             # 非播放器不算尝试

    def test_chain_bottoms_out_at_last_variant(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        for _ in range(5):                                  # 远多于档位数
            self.fx.age_last_attempt(server.RETRY_MIN_GAP + 1)
            self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(self.fx.state()["level"], len(self.fx.variants) - 1)

    def test_fallback_logged_on_token_requests(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        fb = [e["fallback"] for e in self.fx.events()
              if e["event"] == "request" and "fallback" in e]
        self.assertEqual([f["via"] for f in fb], ["base", "token"])
        self.assertFalse(fb[0]["is_player"])
        self.assertTrue(fb[1]["is_player"])


CDN = "https://cdn.example.test/vrc/"
MIB = 1024 * 1024
# 进度判定只看偏移与两个阈值的相对关系；把阈值缩小到 MB 级，首档文件 8MB 即可
# 同时含有"片中"(1MB~6MB)与"尾部索引区"(最后 2MB)，省得每个用例写 200MB 文件。
SUSTAINED = 1 * MIB
TAIL = 2 * MIB
BIG = 8 * MIB
MID = 4 * MIB          # 片中偏移：>= SUSTAINED 且 < BIG - TAIL


class CdnModeTests(unittest.TestCase):
    """CDN 模式：播放器只拿 302，字节由 CDN 出；退档靠时间窗 + Range 偏移证据。"""

    def setUp(self):
        self._saved = (server.SUSTAINED_THRESHOLD, server.PROGRESS_TAIL_IGNORE)
        server.SUSTAINED_THRESHOLD, server.PROGRESS_TAIL_IGNORE = SUSTAINED, TAIL
        self.fx = ProbeFixture(cdn_base=CDN, sizes={"v_av1.mp4": BIG})

    def tearDown(self):
        self.fx.close()
        server.SUSTAINED_THRESHOLD, server.PROGRESS_TAIL_IGNORE = self._saved

    def test_base_still_redirects_to_local_token(self):
        """入口必须落在本机的 token 地址上，yt-dlp 才会把本机地址交给播放器。"""
        status, headers, _ = self.fx.request("/fb/demo", ua=YTDLP_UA)
        self.assertEqual(status, 302)
        self.assertTrue(headers["location"].startswith("/t/"))

    def test_ytdlp_gets_200_from_token_not_redirect(self):
        path = self.fx.resolve_token_path()
        status, headers, _ = self.fx.request(path, ua=YTDLP_UA, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["content-length"], str(BIG))

    def test_player_is_redirected_to_cdn(self):
        path = self.fx.resolve_token_path()
        status, headers, body = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], CDN + "v_av1.mp4")
        self.assertEqual(body, b"")
        fb = [e["fallback"] for e in self.fx.events()
              if e["event"] == "request" and e.get("fallback", {}).get("via") == "token"][-1]
        self.assertTrue(fb["cdn"])
        self.assertEqual(fb["redirect_to"], CDN + "v_av1.mp4")

    def test_retry_within_window_steps_down_to_cdn_next_variant(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        self.fx.age_last_attempt(8)                         # VRChat 冷却 ~5s 后的自动重试
        status, headers, _ = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], CDN + "v_vp9.webm")
        self.assertEqual(self.fx.state()["level"], 1)

    def test_restart_after_max_gap_is_new_play_not_retry(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        self.fx.age_last_attempt(server.RETRY_MAX_GAP + 1)  # 播了很久之后又从头
        status, headers, _ = self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], CDN + "v_av1.mp4")
        self.assertEqual(self.fx.state()["level"], 0)

    def test_mid_file_range_counts_as_progress_and_vetoes_step_down(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        # 播放器把片中的 Range 打回 token 地址（>= 阈值、不在尾部索引区）
        off = MID
        status, _, _ = self.fx.request(path, ua=PLAYER_UA, rng=f"bytes={off}-")
        self.assertEqual(status, 302)                       # 依旧被送去 CDN
        self.assertEqual(self.fx.state()["best_bytes"], off)
        self.fx.age_last_attempt(8)
        self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(self.fx.state()["level"], 0)       # 有进度证据，不退档
        fb = [e["fallback"] for e in self.fx.events()
              if e["event"] == "request" and e.get("fallback", {}).get("progress_offset")]
        self.assertEqual(fb[0]["progress_offset"], off)

    def test_tail_index_range_is_not_progress(self):
        path = self.fx.resolve_token_path()
        self.fx.request(path, ua=PLAYER_UA)
        self.fx.request(path, ua=PLAYER_UA, rng=f"bytes={BIG - TAIL // 2}-")  # 读尾部 moov
        self.assertEqual(self.fx.state()["best_bytes"], 0)
        self.fx.age_last_attempt(8)
        self.fx.request(path, ua=PLAYER_UA)
        self.assertEqual(self.fx.state()["level"], 1)       # 只读过索引 → 仍判为失败

    def test_progress_on_old_level_token_does_not_vouch_for_new_level(self):
        path = self.fx.resolve_token_path()                 # av1 token
        self.fx.request(path, ua=PLAYER_UA)
        self.fx.age_last_attempt(8)
        self.fx.request(path, ua=PLAYER_UA)                 # 退到 vp9
        self.assertEqual(self.fx.state()["level"], 1)
        self.fx.request(path, ua=PLAYER_UA, rng=f"bytes={MID}-")  # 旧 av1 token 的片中读
        self.assertEqual(self.fx.state()["best_bytes"], 0)

    def test_plain_files_still_served_locally_in_cdn_mode(self):
        """CDN 模式只改 /t 对播放器的行为；直链 /file 仍由本机 serve（探针用途）。"""
        status, _, body = self.fx.request("/v_h264.mp4", ua=PLAYER_UA)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 4096)


if __name__ == "__main__":
    unittest.main()
