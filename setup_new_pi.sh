#!/usr/bin/env bash
# =============================================================================
#  Neuroom Chamber - New Raspberry Pi setup script
#  Target : Raspberry Pi 4 + Raspberry Pi OS (64-bit) "Trixie"
#  Repo   : https://github.com/Moo-75/Neuroom_chamber
#
#  실행:
#     chmod +x setup_new_pi.sh
#     ./setup_new_pi.sh
#  실행 후 반드시 재부팅:  sudo reboot
#
#  ※ Phase 0(Imager에서 사용자명 pi / WiFi KR / 시간대 Asia-Seoul)와
#    Phase 2(디스플레이 180도 회전)는 GUI/수동이라 이 스크립트에 없음.
#    SETUP.md 참고.
# =============================================================================
set -euo pipefail

REPO="https://github.com/Moo-75/Neuroom_chamber.git"
CLONE_DIR="${HOME}/Desktop/Neuroom_chamber"

echo "================================================================"
echo " Neuroom Chamber 새 Pi 세팅 시작"
echo "================================================================"

# -----------------------------------------------------------------------------
# 1) 필수 라이브러리 (apt 사용 → PEP668 'externally-managed' 우회, venv 불필요)
#    - python3-rpi-lgpio : RPi.GPIO 호환(lgpio 백엔드). 코드 수정 없이 동작
#    - python3-pygame    : 디스플레이 cue
#    - python3-opencv    : cv2, USB 웹캠 녹화
#    - python3-serial    : pyserial, Arduino 통신
#    - python3-pytz      : Asia/Seoul 타임스탬프
#    - python3-numpy     : 수치 연산
# -----------------------------------------------------------------------------
echo ""
echo "[1/6] 라이브러리 설치 ..."
sudo apt update
if ! sudo apt install -y git \
        python3-rpi-lgpio python3-pygame python3-opencv \
        python3-serial python3-pytz python3-numpy ; then
    echo "  ! apt 설치 중 일부 실패. python3-rpi-lgpio 가 없으면 아래로 대체:"
    echo "     sudo apt install -y python3-lgpio"
    echo "     pip install rpi-lgpio --break-system-packages"
    echo "     (주의: 진짜 RPi.GPIO 와 절대 공존시키지 말 것)"
fi

# -----------------------------------------------------------------------------
# 2) 하드웨어 그룹 권한 (GPIO / 시리얼 / 카메라 / I2C / SPI / 오디오 / 입력)
# -----------------------------------------------------------------------------
echo ""
echo "[2/6] 사용자(${USER}) 그룹 권한 추가 ..."
sudo usermod -aG gpio,dialout,video,i2c,spi,audio,input "${USER}"

# -----------------------------------------------------------------------------
# 3) Arduino(USB) 시리얼 → /dev/arduino 심볼릭 링크 udev 규칙
#    Arduino Uno R3 = VID 2341 / PID 0043.
#    ENV{ID_MM_DEVICE_IGNORE}="1" 로 ModemManager 간섭도 동시에 차단.
#    ! 보드를 교체했다면 serial=="14101" 을 새 보드 값으로 바꾸거나 그 조건 삭제.
# -----------------------------------------------------------------------------
echo ""
echo "[3/6] Arduino udev 규칙(/dev/arduino) 설치 ..."
sudo tee /etc/udev/rules.d/99-arduino.rules >/dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0043", ATTRS{serial}=="14101", SYMLINK+="arduino", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload && sudo udevadm trigger

# -----------------------------------------------------------------------------
# 4) ModemManager 비활성화 (새로 꽂힌 /dev/ttyACM* 를 모뎀으로 오인해 점유 방지)
# -----------------------------------------------------------------------------
echo ""
echo "[4/6] ModemManager 비활성화 ..."
sudo systemctl disable --now ModemManager 2>/dev/null || true

# -----------------------------------------------------------------------------
# 5) 시스템 인터페이스 설정 (raspi-config 비대화형)
#    - 시리얼 하드웨어 ON  : enable_uart=1
#    - 시리얼 로그인 콘솔 ON: PuTTY 시리얼 접속(GPIO14/15 -> USB-TTL -> COM)
#    - 데스크톱 자동로그인  : pygame 풀스크린이 뜨려면 로그인된 GUI 세션 필요
#    - 시간대 / WiFi 국가   : Imager에서 이미 했으면 중복이라도 무해
# -----------------------------------------------------------------------------
echo ""
echo "[5/6] 시리얼 콘솔 / 자동로그인 / 시간대 / WiFi 국가 설정 ..."
if sudo raspi-config nonint do_serial_hw 0 2>/dev/null && \
   sudo raspi-config nonint do_serial_cons 0 2>/dev/null ; then
    echo "  - 시리얼(하드웨어+콘솔) 활성화 완료"
else
    echo "  - 신형 함수 없음 → 구형 do_serial 로 대체"
    sudo raspi-config nonint do_serial 0 || true
fi
sudo raspi-config nonint do_boot_behaviour B4 || true   # 데스크톱 자동로그인
sudo raspi-config nonint do_change_timezone Asia/Seoul || true
sudo raspi-config nonint do_wifi_country KR || true

# -----------------------------------------------------------------------------
# 6) 챔버 코드 클론(또는 갱신)
# -----------------------------------------------------------------------------
echo ""
echo "[6/6] 챔버 코드 받기 -> ${CLONE_DIR}"
mkdir -p "$(dirname "${CLONE_DIR}")"
if [ -d "${CLONE_DIR}/.git" ]; then
    git -C "${CLONE_DIR}" pull --ff-only || true
else
    git clone "${REPO}" "${CLONE_DIR}"
fi

echo ""
echo "================================================================"
echo " 완료. 이제 재부팅하세요:   sudo reboot"
echo ""
echo " 재부팅 후 남은 수동 작업(SETUP.md Phase 2):"
echo "   - 디스플레이 180도 회전 (Screen Configuration 또는 cmdline video=...)"
echo "   - (화면 깨질 때만) Wayland -> X11 전환"
echo ""
echo " 검증:"
echo "   ls -l /dev/arduino   &&   cd ${CLONE_DIR}"
echo "   python3 test_GPIO.py ; python3 maze.py ; python3 maintemp.py"
echo "================================================================"
