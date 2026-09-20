#!/usr/bin/env bash
# 生成一段 8 秒测试图案(testsrc + 440Hz 正弦音)，分别编码成 av1/vp9/h265/h264，
# 用于测试 VRChat 播放器对各编码的解码能力（内容不重要，只看能否解码）。
set -e
cd /opt/vrc-probe/media

IN=(-f lavfi -i "testsrc=duration=8:size=640x360:rate=30"
    -f lavfi -i "sine=frequency=440:duration=8")
Q=(-hide_banner -loglevel error -y)

ffmpeg "${Q[@]}" "${IN[@]}" -c:v libx264 -preset veryfast -pix_fmt yuv420p \
    -c:a aac -shortest demo_h264.mp4
echo "h264 done"

ffmpeg "${Q[@]}" "${IN[@]}" -c:v libx265 -preset veryfast -tag:v hvc1 -pix_fmt yuv420p \
    -c:a aac -shortest demo_h265.mp4
echo "h265 done"

ffmpeg "${Q[@]}" "${IN[@]}" -c:v libvpx-vp9 -b:v 0 -crf 34 -pix_fmt yuv420p \
    -c:a libopus -shortest demo_vp9.webm
echo "vp9 done"

ffmpeg "${Q[@]}" "${IN[@]}" -c:v libsvtav1 -preset 8 -crf 40 -pix_fmt yuv420p \
    -c:a aac -shortest demo_av1.mp4
echo "av1 done"

ls -la demo_av1.mp4 demo_vp9.webm demo_h265.mp4 demo_h264.mp4
