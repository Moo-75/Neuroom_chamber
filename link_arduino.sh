#!/usr/bin/env bash
# link_arduino.sh — 연결된 Arduino 자동 감지 후 /dev/arduino 심볼릭 링크 생성
set -euo pipefail
RULE=/etc/udev/rules.d/99-arduino.rules

echo "시리얼 장치 검색 중..."
mapfile -t PORTS < <(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true)

if [ ${#PORTS[@]} -eq 0 ]; then
    echo "❌ /dev/ttyACM* · /dev/ttyUSB* 없음. Arduino USB 연결 확인."
    exit 1
elif [ ${#PORTS[@]} -eq 1 ]; then
    PORT="${PORTS[0]}"
else
    echo "여러 장치 감지:"
    i=1; for p in "${PORTS[@]}"; do
        d=$(udevadm info -q property -n "$p" | sed -n 's/^ID_VENDOR=//p;s/^ID_MODEL=//p' | paste -sd' ')
        echo "  [$i] $p   $d"; i=$((i+1)); done
    read -rp "Arduino 번호 선택: " sel
    PORT="${PORTS[$((sel-1))]}"
fi
echo "선택: $PORT"

VID=$(udevadm info -q property -n "$PORT" | sed -n 's/^ID_VENDOR_ID=//p')
PID=$(udevadm info -q property -n "$PORT" | sed -n 's/^ID_MODEL_ID=//p')
SER=$(udevadm info -q property -n "$PORT" | sed -n 's/^ID_SERIAL_SHORT=//p')
echo "  VID=$VID  PID=$PID  SERIAL=${SER:-(없음)}"
[ -n "$VID" ] && [ -n "$PID" ] || { echo "❌ VID/PID 읽기 실패"; exit 1; }

echo "규칙 작성: $RULE  (VID/PID 매칭)"
sudo tee "$RULE" >/dev/null <<EOF
# 자동 생성됨 (link_arduino.sh). 감지 보드: VID=$VID PID=$PID SERIAL=${SER:-none}
SUBSYSTEM=="tty", ATTRS{idVendor}=="$VID", ATTRS{idProduct}=="$PID", SYMLINK+="arduino", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF

sudo udevadm control --reload && sudo udevadm trigger
sleep 1
if [ -e /dev/arduino ]; then
    echo "✅ 성공:  $(ls -l /dev/arduino)"
else
    echo "⚠️ /dev/arduino 아직 없음 → Arduino 뺐다 다시 꽂거나 'sudo udevadm trigger' 후 'ls -l /dev/arduino' 재확인."
fi
