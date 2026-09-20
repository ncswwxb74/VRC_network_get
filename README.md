# VRC_network_get — VRChat 视频请求探针

一个零依赖（纯 Python 标准库，≥ 3.9）的 HTTP/HTTPS 视频直链服务器，用来**完整记录 VRChat 世界内视频播放器发出的请求**（请求头原始大小写与顺序、TLS 握手结果、Range 序列、连接复用、重定向跟随），与浏览器请求对比，定位"浏览器能播、游戏里播不了"的原因。

在此基础上实现了一套**按流量特征判断客户端解码能力的自动降级机制**：同一入口 URL 依次尝试 AV1 → VP9 → H265 → H264，播放器解不了就自动退到下一档。已在公网真机验证通过（AVPro/WMF 与 yt-dlp 路径均跟随 302）。

## 背景

- VRChat 世界视频播放器有两个后端：**Unity Video Player** 和 **AVPro Video**（Windows 上基于 Media Foundation，UA 为 `NSPlayer` / `WMFSDK` 系）。ProTV、iwaSync3、USharpVideo 等只是它们的封装。
- 非直链 URL 会先由客户端自带的 yt-dlp 解析（UA 伪装成 Chrome），再把**解析后的 URL** 交给播放器；直链则由播放器后端直接请求。
- 解码在客户端、字节送达之后发生，HTTP 层没有任何能力声明；解码失败只表现为"断开连接 + 约 5s 后从头重试"。本项目就是利用这个行为特征做判定。

## 功能

| 能力 | 说明 |
|---|---|
| 直链 | `GET /<file>` 直接 serve `media/` 下文件，支持 Range，`/` 有索引页 |
| HTTPS | `--tls-port/--tls-cert/--tls-key` 由 Python 直接终止 TLS（不经反代，保住原始头部指纹），记录协商版本 / cipher / ALPN / SNI |
| Fallback | `/fb/<name>` 302 到 `/t/<token>/<file>`；token 每次唯一（免疫 CGNAT）；解码失败重试时退到下一档并再次 302 |
| CDN 模式 | `--cdn-base URL`：本机只做跳转控制，`/t/<token>` 对播放器 302 到 CDN 上的当前档文件，视频字节全部由 CDN/对象存储发出 |
| 日志 | 每次启动写 `logs/session-<时间戳>.jsonl`，每行一个事件，字段说明见 [CLAUDE.md](CLAUDE.md#日志格式分析的核心输入) |

## 快速开始

```powershell
# HTTP 基线（0.0.0.0:8080，媒体目录 media\，日志写入 logs\）
python server.py

# 同时开 HTTPS（证书需被 WMF 信任，自签的不行，用真实域名 + Let's Encrypt）
python server.py --port 8080 --tls-port 8443 --tls-cert cert.pem --tls-key key.pem

# 把视频切成 HLS 放进 media\<name>\（需要 ffmpeg）
python make_hls.py input.mp4 --name demo

# 跑测试（纯 unittest；TLS 那组需要 openssl 命令，没有会自动跳过）
python -m unittest discover -s tests -v
```

VRChat 客户端需开启 **Allow Untrusted URLs**。局域网测试记得放行防火墙端口。

### Fallback 配置

`media/fallback.json`，index 0 = 首选（最省流量 / 最难解码），末尾 = 最兼容兜底：

```json
{"demo": {"variants": ["demo_av1.mp4", "demo_vp9.webm", "demo_h265.mp4", "demo_h264.mp4"]}}
```

世界里填 `http(s)://<host>:<port>/fb/demo`。多编码测试片可用 `deploy/gen_testclips.sh`（ffmpeg testsrc 图案）生成。

降级判据（`server.py` 的 `note_attempt_and_decide`）：同一 IP 的播放器 UA（排除 yt-dlp 探测）在当前档从没有过一次 > 20 MB 的传输，又在 > 3 s 后从头（`bytes=0-` / 无 Range）重来 → 判为解不了，退一档。阈值 `SUSTAINED_THRESHOLD` / `RETRY_MIN_GAP` 在文件头部可调。

### CDN 模式（省掉 VPS 上行流量）

```
yt-dlp  ─► VPS /fb/demo ─302─► VPS /t/<token>/demo_av1.mp4 ─200─► 把 token 地址交给播放器
播放器  ─► VPS /t/<token>/demo_av1.mp4 ─302─► https://cdn.xxx/vrc/demo_av1.mp4   ← 字节从这里出
  解码失败，~5s 后重试
播放器  ─► VPS /t/<token>/demo_av1.mp4 ─302─► https://cdn.xxx/vrc/demo_vp9.webm  ← VPS 判定退档
```

```powershell
python server.py --cdn-base https://cdn.xxx/vrc/
```

- 把变体文件原样放到 CDN / 对象存储的该前缀下（R2 公开桶、Bunny Storage 等，需支持 Range）；**`media/` 下仍要保留同名文件**——yt-dlp 探测时要在本机拿到 200，才会把本机 token 地址（而不是 CDN 地址）交给播放器，之后播放器的重试才打得回本机。
- 本机看不到传输量，降级判据改为**时间窗**：距上次尝试 3 s ~ 60 s 内又从头来 → 判为解码失败重试，退一档；超过 60 s 才从头来的当作正常重播。若播放器把片中的 Range 请求也打回 token 地址，起始偏移会被计为"确实在播"的证据（日志 `fallback.progress_offset`），同样阻止误退档。
- 依赖的前提（同主机场景已在 2026-07 真机验证，跨主机待验证）：WMF 跟随 302；重试打的是原始 token 地址而非跳转目标。建议本机入口走 HTTPS（:8443），避免 http→https 跨协议跳转。

## 部署（Linux / systemd）

见 `deploy/`：`vrc-probe.service` 常驻单元；`caddy-g-block.txt` 仅让 Caddy 为域名签发证书（探针**不走**反代，反代会规范化请求头、抹掉指纹）。证书续期后需 restart 服务。

## 已验证结论（2026-07，真机）

- AVPro/WMF 跟随 302，跨文件（av1 token → vp9 token）也跟。
- yt-dlp 先请求 `/fb/<name>` 并跟随 302，把**token URL** 交给播放器 → 降级必须在 token 端做二次跳转。
- 重试时播放器反复打**缓存的旧 token**，服务器每次把它 302 到新档即可推进。
- 多台机器同场测试：完整链 av1→vp9→h265→h264 逐档退到底、直接播 av1 不降级等各种结局均出现。
- 局限：CGNAT 下同一公网 IP 多人共享状态，可能被同 NAT 里解不了的人拖着一起降级（带宽退化，不是硬失败）。

## 目录

```
server.py            探针服务器（全部逻辑）
make_hls.py          mp4 → HLS 切片工具
media/fallback.json  变体链清单
deploy/              systemd 单元、Caddy 片段、测试片生成脚本
tests/               unittest：fallback 链（本地/CDN 模式）与 TLS 握手记录
logs/                运行日志（不入库）
```
