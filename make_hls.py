"""用 ffmpeg 把一个视频切成 HLS（m3u8 + ts 段），输出到 media/<name>/ 下。

用法:
    python make_hls.py input.mp4 [--name demo] [--seg-time 4] [--copy]

--copy 直接复制码流（快，但源必须是 HLS 兼容的 H.264/AAC）；
默认转码为 H.264 + AAC，保证兼容性。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="生成 HLS 测试流")
    parser.add_argument("input", help="源视频文件")
    parser.add_argument("--name", help="输出名（默认取源文件名）")
    parser.add_argument("--seg-time", type=int, default=4, help="分段时长（秒）")
    parser.add_argument("--copy", action="store_true", help="码流直接复制，不转码")
    args = parser.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("找不到 ffmpeg，请先安装并加入 PATH（如 winget install Gyan.FFmpeg）")

    src = Path(args.input).resolve()
    if not src.is_file():
        sys.exit(f"源文件不存在: {src}")

    name = args.name or src.stem
    out_dir = Path(__file__).parent / "media" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    codec_args = (["-c", "copy"] if args.copy
                  else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-b:a", "128k"])
    cmd = [
        ffmpeg, "-y", "-i", str(src), *codec_args,
        "-hls_time", str(args.seg_time),
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / f"{name}_%04d.ts"),
        str(out_dir / f"{name}.m3u8"),
    ]
    print("运行:", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"ffmpeg 失败，退出码 {result.returncode}")
    print(f"\n完成。播放列表 URL 路径: /{name}/{name}.m3u8")


if __name__ == "__main__":
    main()
