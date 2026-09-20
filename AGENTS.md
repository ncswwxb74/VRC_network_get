# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 项目目标

分析 VRChat 游戏内两种视频播放器请求网络视频时的具体行为，定位"游戏内无法播放但浏览器可以正常播放"的原因。

核心思路：搭建一个提供视频直链的 HTTP(S) 服务器，让 VRChat 世界中的视频播放器请求该服务器上的视频，完整记录并分析游戏侧发出的请求（请求头、TLS 握手、Range 行为、重定向跟随等），与浏览器的请求做对比。

**当前状态：`server.py` 已支持 HTTP 直链、HTTPS 直接终止 TLS（记录握手细节）、以及 H265→H264 fallback（token 跳转）。已部署到公网 sakura VPS（HTTP :8080 / HTTPS :8443，域名 g.yxu33.com，用 Caddy 签的 Let's Encrypt 证书）。下一步是真机测试 fallback 的几个未验证行为（播放器是否跟 302、yt-dlp 传原始还是解析后 URL、重试打哪里）。运维/连接细节见记忆库。**

## Python 环境

- conda 装在 `C:\ProgramData\miniconda3`，**不在 PATH 中**，需用完整路径调用：`& "C:\ProgramData\miniconda3\Scripts\conda.exe" ...`
- 项目环境名为 `vrc-net`（Python 3.12），位于 `C:\Users\mbp\.conda\envs\vrc-net`
- 运行脚本直接用解释器完整路径最可靠：`& "C:\Users\mbp\.conda\envs\vrc-net\python.exe" script.py`
- 装包：`& "C:\Users\mbp\.conda\envs\vrc-net\python.exe" -m pip install <pkg>`

## 常用命令

```powershell
# 启动探针服务器（默认 0.0.0.0:8080，媒体目录 media\，日志写入 logs\）
& "C:\Users\mbp\.conda\envs\vrc-net\python.exe" server.py

# 同时开 HTTP 和 HTTPS（直接终止 TLS，可直接指向 Caddy 签好的证书文件）
& "C:\Users\mbp\.conda\envs\vrc-net\python.exe" server.py --port 8080 `
    --tls-port 8443 --tls-cert cert.pem --tls-key key.pem

# 把视频切成 HLS（m3u8+ts）放进 media\<name>\ ——需要 ffmpeg（本机已通过
# winget install Gyan.FFmpeg 安装，已在 PATH）
& "C:\Users\mbp\.conda\envs\vrc-net\python.exe" make_hls.py input.mp4 --name demo
```

### Fallback 机制（有序变体链，逐档下退）

- 变体链写在 `media/fallback.json`，index 0 = 首选（最省流量/最难解码），末尾 = 最兼容兜底。当前 demo：`{"demo": {"variants": ["demo_av1.mp4", "demo_vp9.webm", "demo_h265.mp4", "demo_h264.mp4"]}}`（旧的 `primary`/`fallback` 两档写法仍兼容，会归一化成 variants）。
- 世界里填 `/fb/<name>`（如 `.../fb/demo`），服务器 302 到 `/t/<token>/<file>`；token 每次唯一，免疫 CGNAT。
- 降级判据在 `note_attempt_and_decide`：同一 IP 的播放器（NSPlayer/WMFSDK/Unity UA，排除 yt-dlp 探测）"从头再来"且在当前档从没有过一次 >20MB 的传输 → 判为该编码解不了 → 往下退一档，直到能播或到最兼容档。阈值 `SUSTAINED_THRESHOLD` / `RETRY_MIN_GAP` 在文件头部常量区可调。
- 逻辑改动后的验证方式：起线程内服务器 + http.client 驱动，断言 302 目标与 serve 内容（2026-07 曾以此验证 fallback 链 8 项 + TLS 4 项；当时的测试脚本放在会话 scratchpad 里已丢失，若需重跑请重建到 `tests/` 并入库）。
- 生成多编码测试片：服务器上已装 ffmpeg，`deploy/gen_testclips.sh` 用 testsrc 图案生成 av1/vp9/h265/h264 四档短片到 `media/`（解码能力测试只看编码、不看内容）。

- 服务器零第三方依赖（纯标准库），跨平台，**要求 Python ≥ 3.9**（用了 `Path.is_relative_to`）。浏览器打开 `http://<IP>:8080/` 有文件索引页。
- Linux 部署：把 `server.py` 放到 `/opt/vrc-probe/`，用 `deploy/vrc-probe.service`（systemd）常驻运行；日志同样写在工作目录 `logs/` 下，分析时拉回本地或远程读取均可。
- 局域网测试需放行防火墙（管理员）：`New-NetFirewallRule -DisplayName "VRC probe 8080" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080`
- `media\_smoke\` 下是冒烟测试文件（sample.bin / test.m3u8），可用于连通性检查，可随时删除。

## 日志格式（分析的核心输入）

每次启动写一个 `logs\session-<时间戳>.jsonl`，每行一个 JSON 事件：

- `connection_open` / `connection_close`：连接级事件，含 `conn`（连接序号）、客户端 IP:端口、`requests_served`（用于观察连接复用）。
- `request_start`：收到请求后立即写入（含请求行与全部头），防止长时间流式传输中服务器停止导致请求丢失。
- `request`：请求处理完毕后写入的完整记录，关键字段：
  - `request_line`、`headers`（**保留原始大小写与顺序的数组**——User-Agent 指纹、头顺序差异是重点分析对象）
  - `conn`/`conn_req`：定位请求属于哪条连接、第几个请求
  - `response.status`、`content_range`、`bytes_sent`、`client_aborted`（播放器常中途断开 Range 连接，属正常行为）
  - `file`：命中的媒体文件（posix 风格相对路径），404 时为 null
  - `query`：URL 查询串（不参与文件定位）。多人测试时给每人发 `?tag=名字` 的 URL，按此字段区分测试者（比按 IP 可靠，IP 有 CGNAT 合并问题）
- `protocol_error`：畸形请求行等解析失败，同样有分析价值。
- `connection_open.tls`：HTTPS 连接的握手协商结果 `{version, cipher, alpn, sni}`（非 TLS 连接无此字段）。
- `request.fallback`：走 `/fb` 或 `/t` 的请求带此对象，含 `via`(base/token)、`variant`/`decided_variant`、`token`、`is_player`、`downgraded_now`、以及 token 端二次跳时的 `redirect_to`——分析 fallback 是否按预期触发的核心字段。

分析流程：同一 URL 分别用 Unity 播放器、AVPro 播放器、浏览器请求后，把对应 jsonl 交给 Codex 对比（按 `client_ip` + User-Agent 区分来源；注意 VRChat 对非直链 URL 会先出现 yt-dlp 的请求，UA 不同于播放器本体）。

## 领域背景（VRChat 视频播放机制）

这些知识对理解本项目至关重要：

- VRChat 世界中的视频播放器有两种底层后端：
  - **Unity Video Player**：使用 Unity 自带的视频/网络栈。
  - **AVPro Video**：Windows 平台上基于 Windows Media Foundation (WMF)，其 HTTP/TLS 行为、User-Agent（如 `NSPlayer`/`WMFSDK` 系）、编解码支持均由 WMF 决定，与浏览器差异很大。
  - 常见世界组件（ProTV、iwaSync3、USharpVideo 等）只是这两种后端的封装。
- 对于非直链 URL（如 YouTube），VRChat 客户端会先调用自带的 yt-dlp（位于 `%LOCALAPPDATA%Low\VRChat\VRChat\Tools\` 下）解析出直链，再交给播放器；**视频直链则由播放器后端直接请求**——本项目主要关注后者。
- 测试时 VRChat 客户端需在设置中开启 "Allow Untrusted URLs"，否则非白名单域名的视频不会加载。
- 范围界定（2026-07 测试结论）：MKV 直链在两种后端 + LAN HTTP 下均可播放；部分观众 H265 播不出属于观众端解码环境问题，**不在本项目排查范围**。
- 已知的常见失败原因（分析时的排查方向）：服务器校验 User-Agent/Referer、302 重定向处理差异、Range 请求支持、Content-Type 要求、TLS 版本/证书链要求、HTTP/2-only 服务、IPv6 解析差异。

## 计划中的架构

1. **视频直链服务器**：在本机/局域网提供视频文件直链，需能被 VRChat 客户端访问（注意 Windows 防火墙与绑定地址）。
2. **请求记录层**：记录每个连接的完整信息——HTTP 请求行与全部请求头、TLS ClientHello 细节（版本、cipher suites、SNI、ALPN）、Range 请求序列、连接复用情况。
3. **对比分析**：同一 URL 分别用 Unity 播放器、AVPro 播放器、浏览器请求，对比三者日志差异。

## 环境注意事项

- 开发与测试均在 Windows 上进行（VRChat 客户端是 Windows 程序），临时文件用 `%TEMP%`，不要用 `/tmp`。
- HTTPS 测试需要 VRChat/WMF 信任的证书——自签证书需导入系统证书存储，或使用真实域名 + 有效证书；这本身也是排查项之一（先用 HTTP 建立基线）。
- 抓包类分析（如需要）可考虑 Wireshark/pktmon 辅助，但首选服务器侧日志，避免解密 TLS 的复杂度。
