#!/usr/bin/env python3
"""
Read-only camera fps probe — NO encoding, NO writing, NO display.

목적: 실제 녹화(video_record_worker)에서 30fps로 설정해도 24-27fps밖에 안 찍히는
      원인이 (A) 카메라/USB/DMA가 그만큼밖에 못 주는 것인지, 아니면
      (B) 영상 인코딩(out.write)이 느려서 프레임이 밀리는 것인지 가른다.

판정:
  - 이 프로브의 read-only fps 가 ~30 으로 안정적이다  → 카메라는 멀쩡.
    실제 녹화가 24-27인 건 '인코딩(x264enc/mp4v)'이 병목. → 인코더/해상도/fps 조정으로 해결.
  - 이 프로브도 24-27 이하로 낮거나 들쭉날쭉, max_read 가 크다(수백 ms~초) → 카메라/USB/DMA가 병목.
    → 카메라 읽기 경로 문제(우리가 계속 쫓던 stall)와 동일 원인.

사용:
    python3 cam_fps_probe.py           # 30fps로 60초
    python3 cam_fps_probe.py 15 120    # 15fps로 120초
"""
import sys
import time

import cv2

TARGET_FPS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

if not cap.isOpened():
    print("camera open failed")
    sys.exit(1)

print(
    f"target_fps={TARGET_FPS}  negotiated_fps={cap.get(cv2.CAP_PROP_FPS)}  "
    f"size={int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}",
    flush=True,
)

t0 = time.time()
hb_time = t0
hb_count = 0
count = 0
fails = 0
max_read = 0.0

while time.time() - t0 < DURATION:
    rs = time.time()
    ok, frame = cap.read()
    read_ms = (time.time() - rs) * 1000.0
    if not ok or frame is None:
        fails += 1
        print(f"[{time.strftime('%H:%M:%S')}] read FAIL (blocked {read_ms:.0f}ms, total fails {fails})", flush=True)
        continue
    count += 1
    if read_ms > max_read:
        max_read = read_ms
    now = time.time()
    if now - hb_time >= 5.0:
        fps = (count - hb_count) / (now - hb_time)
        print(
            f"[{time.strftime('%H:%M:%S')}] read-only fps={fps:5.1f}  "
            f"max_read={max_read:4.0f}ms  fails={fails}",
            flush=True,
        )
        hb_time = now
        hb_count = count
        max_read = 0.0

cap.release()
print(f"done: {count} frames read, {fails} fails in {DURATION:.0f}s", flush=True)
